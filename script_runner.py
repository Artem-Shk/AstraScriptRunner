#!/usr/bin/env python3
"""script_runner -- console utility for running bash scripts from a folder.

A Linux/POSIX command-line tool that scans a directory (optionally recursively)
for ``.sh`` / ``.bash`` files, filters and sorts them, then executes every
script through ``bash`` using :mod:`subprocess`. stdout/stderr of every script
is saved to individual log files, return codes are handled and a failure of one
script does not stop the remaining ones.

Run ``python3 script_runner.py --help`` for the full CLI reference.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as _dt
import fnmatch
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_SUFFIXES = (".sh", ".bash")
VALID_MODES = ("sequential", "parallel")
VALID_SORTS = ("name", "mtime", "size")

# Built-in defaults. CLI arguments override config file values, which in turn
# override these defaults.
DEFAULTS: dict = {
    "directory": None,
    "filter": None,
    "mode": "sequential",
    "recursive": False,
    "force": False,
    "sort": "name",
    "reverse": False,
    "jobs": None,
    "timeout": None,
    "output_dir": "logs",
    "dry_run": False,
    "verbose": False,
    "log": True,
    "continue_on_error": True,
    "script_args": [],
}

LOGGER = logging.getLogger("script_runner")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Script:
    """A single discovered script.

    Attributes:
        path: Absolute path to the script file.
        size: File size in bytes.
        mtime: Last modification time (POSIX timestamp).
    """

    path: Path
    size: int
    mtime: float

    @property
    def name(self) -> str:
        """File name (without directory part)."""
        return self.path.name

    @property
    def executable(self) -> bool:
        """Whether the current user may execute this file (POSIX exec bit)."""
        return os.access(self.path, os.X_OK)


@dataclasses.dataclass
class RunResult:
    """Outcome of running a single script."""

    script: Script
    returncode: int
    duration: float
    logfile: Optional[Path]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """True when the script finished with exit code 0 and no launch error."""
        return self.error is None and self.returncode == 0


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class RunnerError(Exception):
    """Recoverable, user-facing error with a clear message."""


# ---------------------------------------------------------------------------
# Configuration handling
# ---------------------------------------------------------------------------


def load_config(path: Optional[str]) -> dict:
    """Load a JSON or YAML configuration file.

    Args:
        path: Path to ``*.json`` / ``*.jsonc`` / ``*.yaml`` / ``*.yml`` file,
            or None to skip loading.

    Returns:
        Mapping of configuration keys. Unknown keys are ignored later.

    Raises:
        RunnerError: If the file is missing, unreadable or malformed.
    """
    if not path:
        return {}

    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise RunnerError(f"Config file not found: {cfg_path}")

    suffix = cfg_path.suffix.lower()
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunnerError(f"Cannot read config file {cfg_path}: {exc}") from exc

    try:
        if suffix in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise RunnerError(
                    "YAML config requires the 'PyYAML' package "
                    "(pip install pyyaml), or use a JSON config instead."
                ) from exc
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw) if raw.strip() else {}
    except RunnerError:
        raise
    except Exception as exc:  # json.JSONDecodeError, yaml.YAMLError, ...
        raise RunnerError(f"Invalid config file {cfg_path}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RunnerError(f"Config file {cfg_path} must contain a mapping of options.")
    return data


def merge_config(args: argparse.Namespace, config: dict) -> dict:
    """Combine built-in defaults, config file values and CLI arguments.

    Precedence (highest first): CLI argument -> config file -> built-in default.

    Args:
        args: Parsed CLI arguments (options default to None when unset).
        config: Values loaded from the config file.

    Returns:
        Fully resolved settings dictionary.
    """
    resolved = dict(DEFAULTS)
    for key in DEFAULTS:
        cli_value = getattr(args, key, None)
        if cli_value is not None:
            resolved[key] = cli_value
        elif key in config and config[key] is not None:
            resolved[key] = config[key]
    return resolved


# ---------------------------------------------------------------------------
# Discovery / filtering / sorting
# ---------------------------------------------------------------------------


def iter_candidate_files(directory: Path, recursive: bool) -> Iterable[Path]:
    """Yield every regular file that has a shell-script suffix.

    Args:
        directory: Root directory to scan.
        recursive: If True also descend into sub-directories.

    Yields:
        Paths of candidate script files.
    """
    walker = directory.rglob("*") if recursive else directory.glob("*")
    for entry in walker:
        try:
            if entry.is_file() and entry.suffix.lower() in SCRIPT_SUFFIXES:
                yield entry
        except OSError as exc:  # broken symlink, permission problem, ...
            LOGGER.debug("Skipping %s: %s", entry, exc)


def matches_filter(name: str, pattern: Optional[str]) -> bool:
    """Check whether a file name matches a user supplied filter.

    Supported filter forms:
        * ``.sh`` / ``bash``  -- extension (case-insensitive, optional dot).
        * ``deploy*``         -- glob pattern (``fnmatch``).
        * ``backup``          -- plain substring.

    Args:
        name: File name to test.
        pattern: Filter string, or None to accept everything.

    Returns:
        True if the file should be kept.
    """
    if not pattern:
        return True

    lowered_name = name.lower()
    lowered_pattern = pattern.lower()
    stem = lowered_pattern[1:] if lowered_pattern.startswith(".") else lowered_pattern

    # Extension match, e.g. "--filter .sh".
    if "." not in pattern.strip(".") and len(stem) <= 5:
        if lowered_name.endswith("." + stem):
            return True

    glob_like = any(ch in pattern for ch in "*?[]")
    if glob_like:
        return fnmatch.fnmatch(lowered_name, lowered_pattern)

    return lowered_pattern in lowered_name


def sort_scripts(scripts: list[Script], key: str, reverse: bool = False) -> list[Script]:
    """Return scripts sorted by the requested key.

    Args:
        scripts: Discovered scripts.
        key: One of ``name``, ``mtime`` or ``size``.
        reverse: Sort descending when True.

    Returns:
        New sorted list.

    Raises:
        RunnerError: If *key* is not recognised.
    """
    keyfuncs = {
        "name": lambda s: s.name.lower(),
        "mtime": lambda s: s.mtime,
        "size": lambda s: s.size,
    }
    if key not in keyfuncs:
        raise RunnerError(
            f"Unknown sort key '{key}'. Use one of: {', '.join(VALID_SORTS)}."
        )
    return sorted(scripts, key=keyfuncs[key], reverse=reverse)


def discover_scripts(
    directory: Path,
    recursive: bool,
    name_filter: Optional[str],
    sort_key: str,
    reverse: bool,
    force: bool,
) -> list[Script]:
    """Discover, filter and sort scripts inside *directory*.

    Args:
        directory: Root directory to scan.
        recursive: Scan sub-directories too.
        name_filter: Optional name/extension filter.
        sort_key: Sort key (``name`` / ``mtime`` / ``size``).
        reverse: Reverse the sort order.
        force: Keep non-executable files as well.

    Returns:
        Sorted list of :class:`Script` objects.

    Raises:
        RunnerError: If the directory is invalid or missing.
    """
    if not directory.exists():
        raise RunnerError(f"Directory does not exist: {directory}")
    if not directory.is_dir():
        raise RunnerError(f"Not a directory: {directory}")

    found: list[Script] = []
    skipped_not_executable = 0

    for path in iter_candidate_files(directory, recursive):
        if not matches_filter(path.name, name_filter):
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            LOGGER.warning("Cannot stat %s: %s", path, exc)
            continue

        script = Script(path=path.resolve(), size=stat.st_size, mtime=stat.st_mtime)
        if not force and not script.executable:
            skipped_not_executable += 1
            LOGGER.debug("Not executable, skipping: %s", path)
            continue
        found.append(script)

    if skipped_not_executable:
        LOGGER.info(
            "%d script(s) skipped because they are not executable "
            "(use --force to run them anyway).",
            skipped_not_executable,
        )

    return sort_scripts(found, sort_key, reverse)


# ---------------------------------------------------------------------------
# Bash discovery
# ---------------------------------------------------------------------------


def find_bash() -> str:
    """Locate the ``bash`` interpreter (Linux/POSIX).

    Search order:
        1. ``bash`` in ``$PATH`` (``which``-style lookup);
        2. common absolute locations used by Linux distributions.

    Returns:
        Path to the interpreter.

    Raises:
        RunnerError: If no bash executable can be found.
    """
    found = shutil.which("bash")
    if found:
        return found

    for candidate in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"):
        if os.access(candidate, os.X_OK):
            return candidate

    raise RunnerError(
        "Cannot find 'bash' in PATH. Install it, e.g. "
        "'sudo apt-get install -y bash' (Debian/Ubuntu) or "
        "'sudo dnf install -y bash' (Fedora/RHEL)."
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ScriptRunner:
    """Runs a list of discovered scripts and collects their results."""

    def __init__(self, settings: dict) -> None:
        """Store resolved settings and prepare the log directory.

        Args:
            settings: Fully resolved settings (see :data:`DEFAULTS`).

        Raises:
            RunnerError: If the script directory or output directory is invalid.
        """
        self.settings = settings
        self.directory = Path(settings["directory"]).expanduser().resolve()
        self.output_dir = Path(settings["output_dir"]).expanduser()
        self.bash = find_bash()
        self._validate()

    # -- validation ---------------------------------------------------------

    def _validate(self) -> None:
        """Validate settings, raising a clear error when something is wrong."""
        if self.settings["mode"] not in VALID_MODES:
            raise RunnerError(
                f"Invalid mode '{self.settings['mode']}'. "
                f"Use one of: {', '.join(VALID_MODES)}."
            )
        if self.settings["sort"] not in VALID_SORTS:
            raise RunnerError(
                f"Invalid sort '{self.settings['sort']}'. "
                f"Use one of: {', '.join(VALID_SORTS)}."
            )
        if self.settings["jobs"] is not None and int(self.settings["jobs"]) < 1:
            raise RunnerError("--jobs must be a positive integer.")
        if self.settings["timeout"] is not None and float(self.settings["timeout"]) <= 0:
            raise RunnerError("--timeout must be a positive number of seconds.")

    # -- public API ---------------------------------------------------------

    def run(self) -> int:
        """Execute the whole workflow.

        Returns:
            Process exit code: 0 if everything succeeded, 1 if any script
            failed, 2 when no scripts were found.
        """
        scripts = discover_scripts(
            directory=self.directory,
            recursive=bool(self.settings["recursive"]),
            name_filter=self.settings["filter"],
            sort_key=self.settings["sort"],
            reverse=bool(self.settings["reverse"]),
            force=bool(self.settings["force"]),
        )

        if not scripts:
            LOGGER.warning("No matching scripts found in: %s", self.directory)
            return 2

        LOGGER.info("Found %d script(s) to process.", len(scripts))
        if self.settings["dry_run"]:
            self._print_dry_run(scripts)
            return 0

        if self.settings["log"]:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            LOGGER.info("Logs will be written to: %s", self.output_dir.resolve())

        if self.settings["mode"] == "parallel":
            results = self._run_parallel(scripts)
        else:
            results = self._run_sequential(scripts)

        return self._summarize(results)

    # -- dry run ------------------------------------------------------------

    def _print_dry_run(self, scripts: Sequence[Script]) -> None:
        """Print the plan without executing anything."""
        LOGGER.info("Dry run -- the following scripts would be executed:")
        for index, script in enumerate(scripts, start=1):
            flags = [] if script.executable else ["(not executable)"]
            print(f"  {index:>3}. {script.path} {' '.join(flags)}".rstrip())
        if self.settings["script_args"]:
            print(f"  Arguments passed to each script: "
                  f"{self.settings['script_args']}")
        print(f"  Mode: {self.settings['mode']}")
        if self.settings["mode"] == "parallel":
            print(f"  Jobs: {self._job_count(len(scripts))}")

    # -- execution ----------------------------------------------------------

    def _run_sequential(self, scripts: Sequence[Script]) -> list[RunResult]:
        """Run scripts one after another."""
        results: list[RunResult] = []
        for script in scripts:
            result = self._run_one(script)
            results.append(result)
            if not result.ok and not self.settings["continue_on_error"]:
                LOGGER.error("Stopping after failure (continue-on-error disabled).")
                break
        return results

    def _run_parallel(self, scripts: Sequence[Script]) -> list[RunResult]:
        """Run scripts concurrently using a thread pool."""
        workers = self._job_count(len(scripts))
        LOGGER.info("Running %d script(s) with up to %d parallel jobs.",
                    len(scripts), workers)
        results: list[RunResult] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(self._run_one, s): s for s in scripts}
            for future in concurrent.futures.as_completed(future_map):
                script = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # defensive: never lose a result
                    LOGGER.error("Unexpected failure for %s: %s", script.path, exc)
                    results.append(
                        RunResult(script, returncode=-1, duration=0.0,
                                  logfile=None, error=str(exc))
                    )
        return results

    def _job_count(self, total: int) -> int:
        """Return the effective number of parallel workers."""
        jobs = self.settings["jobs"]
        if jobs is None:
            return max(1, min(total, (os.cpu_count() or 1)))
        return max(1, min(int(jobs), total or 1))

    def _run_one(self, script: Script) -> RunResult:
        """Execute a single script, capturing output to a log file.

        Args:
            script: Script to run.

        Returns:
            A :class:`RunResult` describing the outcome. Launch failures are
            reported via :attr:`RunResult.error` instead of raising, so that
            the caller can continue with the remaining scripts.
        """
        cmd = [self.bash, str(script.path), *[str(a) for a in self.settings["script_args"]]]
        LOGGER.info("START  %s", script.path)
        LOGGER.debug("Command: %s", " ".join(shlex.quote(part) for part in cmd))

        logfile = self._logfile_for(script)
        started = _dt.datetime.now().timestamp()
        stdout = stderr = ""
        returncode = -1
        error: Optional[str] = None

        try:
            completed = subprocess.run(
                cmd,
                cwd=str(script.path.parent),
                capture_output=True,
                text=True,
                timeout=self.settings["timeout"],
                check=False,
            )
            stdout, stderr = completed.stdout or "", completed.stderr or ""
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            error = f"Timed out after {self.settings['timeout']}s"
            returncode = -9
        except (OSError, ValueError) as exc:
            error = f"Failed to launch: {exc}"
        except Exception as exc:  # noqa: BLE001 - keep the batch alive
            error = f"Unexpected error: {exc}"

        duration = _dt.datetime.now().timestamp() - started

        if self.settings["log"]:
            try:
                self._write_log(logfile, script, cmd, stdout, stderr, returncode, error, duration)
            except OSError as exc:
                error = (error + "; " if error else "") + f"cannot write log: {exc}"
                logfile = None

        result = RunResult(script, returncode, duration, logfile, error)
        if result.ok:
            LOGGER.info("OK     %s (%.2fs)", script.path, duration)
        elif error:
            LOGGER.error("FAIL   %s: %s", script.path, error)
        else:
            LOGGER.error("FAIL   %s (exit code %d, %.2fs)", script.path, returncode, duration)
        return result

    def _logfile_for(self, script: Script) -> Path:
        """Build a unique log-file path for *script*."""
        relative = script.path.relative_to(self.directory)
        safe = "_".join(relative.with_suffix("").parts)
        timestamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        return self.output_dir / f"{safe}_{timestamp}.log"

    @staticmethod
    def _write_log(
        logfile: Path,
        script: Script,
        cmd: Sequence[str],
        stdout: str,
        stderr: str,
        returncode: int,
        error: Optional[str],
        duration: float,
    ) -> None:
        """Write a structured log file for one script run."""
        lines = [
            "=" * 70,
            f"Script   : {script.path}",
            f"Command  : {' '.join(shlex.quote(p) for p in cmd)}",
            f"Started  : {_dt.datetime.now().isoformat(timespec='seconds')}",
            f"Duration : {duration:.2f}s",
            f"Exit code: {returncode}",
        ]
        if error:
            lines.append(f"Error    : {error}")
        lines += ["=" * 70, "--- STDOUT ---", stdout.rstrip("\n"), "",
                  "--- STDERR ---", stderr.rstrip("\n"), ""]
        logfile.write_text("\n".join(lines), encoding="utf-8")

    # -- summary ------------------------------------------------------------

    @staticmethod
    def _summarize(results: Sequence[RunResult]) -> int:
        """Log a summary table and return the process exit code."""
        succeeded = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]

        LOGGER.info("Summary: %d succeeded, %d failed, %d total.",
                    len(succeeded), len(failed), len(results))
        for result in failed:
            reason = result.error or f"exit code {result.returncode}"
            LOGGER.error("  FAILED: %s (%s)", result.script.path, reason)

        return 1 if failed else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser for the utility."""
    parser = argparse.ArgumentParser(
        prog="script_runner",
        description="Scan a folder for bash scripts and execute them.",
        epilog=(
            "Arguments after '--' are forwarded to every executed script, e.g.:\n"
            "  script_runner ./scripts --env prod --  --run-checks -v"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("directory", help="Folder that contains the scripts.")
    parser.add_argument("-f", "--filter", metavar="PATTERN",
                        help="Name/extension filter, e.g. '.sh', 'deploy*', 'backup'.")
    parser.add_argument("-m", "--mode", choices=VALID_MODES,
                        help="Execution mode (default: sequential).")
    parser.add_argument("-r", "--recursive", action="store_true", default=None,
                        help="Scan sub-directories recursively.")
    parser.add_argument("--force", action="store_true", default=None,
                        help="Run scripts even without the executable bit.")
    parser.add_argument("-s", "--sort", choices=VALID_SORTS,
                        help="Sort order (default: name).")
    parser.add_argument("--reverse", action="store_true", default=None,
                        help="Reverse the sort order.")
    parser.add_argument("-j", "--jobs", type=int, metavar="N",
                        help="Parallel workers (default: CPU count).")
    parser.add_argument("--timeout", type=float, metavar="SEC",
                        help="Per-script timeout in seconds.")
    parser.add_argument("-o", "--output-dir", metavar="DIR",
                        help="Directory for log files (default: ./logs).")
    parser.add_argument("--no-log", dest="log", action="store_false", default=None,
                        help="Do not write log files.")
    parser.add_argument("--dry-run", action="store_true", default=None,
                        help="Only show which scripts would be executed.")
    parser.add_argument("--no-continue-on-error", dest="continue_on_error",
                        action="store_false", default=None,
                        help="Stop the batch after the first failure.")
    parser.add_argument("-v", "--verbose", action="store_true", default=None,
                        help="Verbose (debug) output.")
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="JSON or YAML configuration file.")
    return parser


def split_argv(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split argv into utility arguments and script arguments at ``--``.

    Args:
        argv: Raw command line arguments (excluding the program name).

    Returns:
        ``(utility_args, script_args)`` tuple.
    """
    if "--" in argv:
        index = argv.index("--")
        return list(argv[:index]), list(argv[index + 1:])
    return list(argv), []


def configure_logging(verbose: bool) -> None:
    """Set up console logging (and optional debug detail)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.DEBUG if verbose else logging.INFO)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Program entry point.

    Args:
        argv: Arguments without the program name (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    utility_argv, script_args = split_argv(argv)

    parser = build_parser()
    args = parser.parse_args(utility_argv)

    try:
        config = load_config(args.config)
        settings = merge_config(args, config)
        settings["script_args"] = script_args or list(settings.get("script_args") or [])
    except RunnerError as exc:
        parser.error(str(exc))  # exits with status 2
        return 2

    configure_logging(bool(settings["verbose"]))

    try:
        runner = ScriptRunner(settings)
        return runner.run()
    except RunnerError as exc:
        LOGGER.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.error("Interrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
