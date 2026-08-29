"""Process manager for model-routed Antigravity CLI workers."""

from __future__ import annotations

import fcntl as fcntl_module
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - plugin manifest supports POSIX hosts only
    fcntl = None

try:
    from plugins.plugin_storage import plugin_data_dir
except ModuleNotFoundError as exc:
    if exc.name not in {"plugins", "plugins.plugin_storage"}:
        raise

    # Fallback for when plugin is loaded outside Hermes core (e.g., during testing)
    def plugin_data_dir(plugin_name: str) -> Path:
        """Fallback plugin data directory when Hermes core is not available."""
        hermes_home = os.environ.get("HERMES_HOME")
        base = Path(hermes_home) if hermes_home else Path.home() / ".hermes"
        return base / "plugin-data" / plugin_name


MODEL_BY_TASK_TYPE = {
    "simple": "gemini-3.7-flash-low",
    "investigation": "gemini-3.7-flash-medium",
    "implementation": "gemini-3.7-flash-high",
    "hard": "gemini-3.1-pro-high",
    "review": "claude-sonnet-4-6",
    "exceptional": "claude-opus-4-6-thinking",
    "cheap_second_opinion": "gpt-oss-120b-medium",
}

ALL_KNOWN_STATUSES = {
    "queued",
    "running",
    "continuing",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "interrupted",
}

TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "interrupted",
}

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_WORKER_ID_RE = re.compile(r"^agy_[A-Za-z0-9_-]{1,128}$")
_RUNTIME_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MINIMUM_AGY_VERSION = (1, 1, 8)

PROCESS_GROUP_TERM_GRACE_SECONDS = 5.0
PROCESS_REAP_TIMEOUT_SECONDS = 1.0
PIPE_DRAIN_TIMEOUT_SECONDS = 1.0
SHUTDOWN_WAIT_SECONDS = 10.0
MAX_SAMPLE_MEMORY_BYTES = 256 * 1024  # 256KB buffer in memory for JSON payload extraction

LEASE_LOCKED = "LOCKED"
LEASE_UNLOCKED = "UNLOCKED"
LEASE_UNKNOWN = "UNKNOWN"

ALLOWLIST_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_COLLATE",
    "TERM",
    "COLORTERM",
    "SSH_AUTH_SOCK",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
}

_AGY_PROBE_CACHE: dict[str, tuple[bool, str | None, str | None]] = {}
_AGY_PROBE_LOCK = threading.Lock()

# Pattern used by the "script" env strategy to strip any var that looks
# like a secret. Order matters: substring match, so place longer/more
# specific fragments first to avoid accidental shadowing.
_SECRET_DENY_SUBSTRINGS: tuple[str, ...] = (
    "HERMES_",
    "ANTHROPIC_",
    "OPENAI_",
    "CLAUDE_",
    "AWS_",
    "GITHUB_TOKEN",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_",
    "PYTHONPATH",
    "PYTHONHOME",
)

# Magic byte signatures that identify a native executable on the major
# platforms we support. We only need a tiny sniff to disambiguate native
# binaries from interpreter shim scripts.
_NATIVE_MAGIC: tuple[bytes, ...] = (
    b"\x7fELF",                  # Linux / BSD ELF
    b"\xfe\xed\xfa\xce",         # Mach-O 32-bit
    b"\xfe\xed\xfa\xcf",         # Mach-O 64-bit
    b"\xfe\xed\xfe\xed",         # Mach-O universal
    b"MZ",                       # Windows PE (we do not run this on win
                                 # but it is not a shim script either)
)


def _classify_agy_target(path: str) -> str:
    """Return 'script' if the agy executable at ``path`` is a shim script
    (any file whose first bytes form a ``#!`` shebang), 'native' otherwise.

    The "native" branch is the safe default: when in doubt, treat the
    target as a closed binary and use the strict allowlist env so Hermes
    secrets never leak to a worker we cannot reason about.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return "native"
    if not head:
        return "native"
    # Shim scripts: any leading whitespace is tolerated, then a '#!'.
    stripped = head.lstrip()
    if stripped.startswith(b"#!"):
        return "script"
    if any(head.startswith(magic) for magic in _NATIVE_MAGIC):
        return "native"
    # Unknown format. Default to the safer allowlist path.
    return "native"


def _build_env_allowlist() -> dict[str, str]:
    """Strict allowlist env. Used for native agy binaries that do not
    need any context from the parent shell."""
    sanitized: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in ALLOWLIST_ENV_KEYS or (k.startswith("LC_") and len(k) < 32):
            if any(bad in k for bad in _SECRET_DENY_SUBSTRINGS):
                continue
            sanitized[k] = v
    return sanitized


def _build_env_denylist() -> dict[str, str]:
    """Inherit the parent env, then drop anything that looks like a
    secret. Used for shim wrappers (mise/asdf/rtx/nix/brew) that need
    loader context to resolve the real agy binary."""
    sanitized: dict[str, str] = {}
    for k, v in os.environ.items():
        if any(bad in k for bad in _SECRET_DENY_SUBSTRINGS):
            continue
        sanitized[k] = v
    return sanitized


def _build_env_for_target(target_path: str, kind: str) -> dict[str, str]:
    """Select the env strategy for a given agy target and kind.

    This is split out from ``_execute_once`` so it can be tested in
    isolation, without spawning a real subprocess.
    """
    if kind == "script":
        return _build_env_denylist()
    return _build_env_allowlist()


def _build_env_for_agy(agy_path: str) -> dict[str, str]:
    """Public dispatch used by ``_execute_once``: classify the agy
    binary, then build the matching env. The kind is computed at spawn
    time so a user who replaces the shim with a real binary does not
    need to restart Hermes to get the new strategy."""
    kind = _classify_agy_target(agy_path)
    return _build_env_for_target(agy_path, kind)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)


def _check_agy_executable(path_or_name: str | None) -> tuple[bool, str | None, str | None]:
    """Verify presence, absolute canonical path, and capability of agy binary."""
    target = path_or_name or "agy"
    which_path = shutil.which(target)
    if which_path is None:
        if os.path.isabs(target) and os.path.isfile(target) and os.access(target, os.X_OK):
            which_path = target
        else:
            return False, None, f"agy executable not found: {target}"

    canonical_path = str(Path(which_path).expanduser().resolve())
    with _AGY_PROBE_LOCK:
        if canonical_path in _AGY_PROBE_CACHE:
            return _AGY_PROBE_CACHE[canonical_path]

    if not os.path.isfile(canonical_path) or not os.access(canonical_path, os.X_OK):
        res = (False, canonical_path, f"Resolved agy path is not an executable file: {canonical_path}")
        with _AGY_PROBE_LOCK:
            _AGY_PROBE_CACHE[canonical_path] = res
        return res

    try:
        proc = subprocess.run(
            [canonical_path, "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        combined_output = f"{proc.stdout} {proc.stderr}"
        match = _VERSION_RE.search(combined_output)
        if match is not None:
            ver = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if ver < MINIMUM_AGY_VERSION:
                res = (
                    False,
                    canonical_path,
                    f"agy version {ver[0]}.{ver[1]}.{ver[2]} is below required minimum 1.1.8",
                )
                with _AGY_PROBE_LOCK:
                    _AGY_PROBE_CACHE[canonical_path] = res
                return res

        if proc.returncode == 0 or match is not None:
            res = (True, canonical_path, None)
            with _AGY_PROBE_LOCK:
                _AGY_PROBE_CACHE[canonical_path] = res
            return res

        res = (True, canonical_path, None)
        with _AGY_PROBE_LOCK:
            _AGY_PROBE_CACHE[canonical_path] = res
        return res
    except Exception as exc:
        res = (True, canonical_path, None)
        with _AGY_PROBE_LOCK:
            _AGY_PROBE_CACHE[canonical_path] = res
        return res


def _fsync_dir(dir_path: Path) -> None:
    """Safely fsync directory descriptor on POSIX."""
    if os.name != "posix":
        return
    try:
        dfd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Safely truncate text to max_bytes without splitting multi-byte UTF-8 code points."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    truncated_bytes = encoded[:max_bytes]
    safe_text = truncated_bytes.decode("utf-8", errors="ignore")
    return safe_text, True


@dataclass
class RunRecord:
    worker_id: str
    owner_id: str
    goal: str
    context: str
    prompt: str
    task_type: str
    model: str
    execution_mode: str
    effort: str | None
    workdir: str
    timeout_seconds: int
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    pgid: int | None = None
    returncode: int | None = None
    conversation_id: str | None = None
    parent_worker_id: str | None = None
    response: str = ""
    response_truncated: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float | None = None
    num_turns: int | None = None
    error: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    steer_queue: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    runtime_id: str | None = None
    supervisor_pid: int | None = None
    supervisor_started_at: str | None = None
    attempt_index: int = 1


@dataclass(frozen=True)
class OwnedProcess:
    """Immutable spawn-time ownership data for one supervised process group."""

    process: subprocess.Popen[str]
    pgid: int | None


class AgyManager:
    """Own bounded background agy subprocesses for one plugin runtime."""

    def __init__(self, settings: dict[str, Any] | None = None, agy_path: str | None = None):
        settings = settings or {}
        self.max_concurrent = self._bounded_int(
            settings.get("max_concurrent", 3), default=3, minimum=1, maximum=10
        )
        self.max_pending_workers = self._bounded_int(
            settings.get("max_pending_workers", 20), default=20, minimum=1, maximum=100
        )
        self.max_pending_per_owner = self._bounded_int(
            settings.get("max_pending_per_owner", 10), default=10, minimum=1, maximum=50
        )
        self.default_timeout = self._bounded_int(
            settings.get("default_timeout_seconds", 600),
            default=600,
            minimum=30,
            maximum=7200,
        )
        self.max_result_chars = self._bounded_int(
            settings.get("max_result_chars", 24000),
            default=24000,
            minimum=2000,
            maximum=100000,
        )
        self.max_records_in_memory = self._bounded_int(
            settings.get("max_records_in_memory", 500),
            default=500,
            minimum=1,
            maximum=10000,
        )
        self.max_raw_output_bytes = self._bounded_int(
            settings.get("max_raw_output_bytes", 16 * 1024 * 1024),
            default=16 * 1024 * 1024,
            minimum=1,
            maximum=1024 * 1024 * 1024,
        )
        self.retention_days = self._bounded_int(
            settings.get("retention_days", 30),
            default=30,
            minimum=0,
            maximum=3650,
        )
        self.max_total_storage_mb = self._bounded_int(
            settings.get("max_total_storage_mb", 1024),
            default=1024,
            minimum=1,
            maximum=1024 * 1024,
        )
        self.allow_accept_edits = self._setting_bool(
            settings.get("allow_accept_edits", False), default=False
        )
        self.max_steer_queue_size = self._bounded_int(
            settings.get("max_steer_queue_size", 10), default=10, minimum=1, maximum=50
        )
        self.max_steer_message_chars = self._bounded_int(
            settings.get("max_steer_message_chars", 24000), default=24000, minimum=100, maximum=100000
        )

        is_valid, canonical_agy, err = _check_agy_executable(agy_path)
        if is_valid and canonical_agy is not None:
            self.agy_path = canonical_agy
        else:
            self.agy_path = str(Path(agy_path).expanduser().resolve()) if agy_path else "agy"

        raw_data_dir = plugin_data_dir("delegate-agy")
        self.data_dir = self._check_confined_dir(raw_data_dir)
        self.runs_dir = self._check_confined_dir(self.data_dir / "runs")
        self.runtime_dir = self._check_confined_dir(self.data_dir / "runtime")

        self.runtime_id = uuid.uuid4().hex
        self.supervisor_pid = os.getpid()
        self.supervisor_started_at = _now()
        self._runtime_lease_fd: int | None = None
        self._lease_lock = threading.Lock()
        self._reaper_thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        try:
            self._acquire_runtime_lease()
            self._lock = threading.RLock()
            self._records: dict[str, RunRecord] = {}
            self._record_cache: OrderedDict[str, RunRecord] = OrderedDict()
            self._record_cache_limit = min(100, self.max_records_in_memory)
            self._processes: dict[str, OwnedProcess] = {}
            self._reap_worker_ids: set[str] = set()
            self._futures: dict[str, Future[None]] = {}
            self._retention_state_lock = threading.Lock()
            self._retention_running = False
            self._retention_pending = False
            self._retention_errors: list[str] = []
            self._closed = False
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_concurrent,
                thread_name_prefix="delegate-agy",
            )
            self._gc_stale_temporary_files()
            self._gc_stale_runtime_locks()
            self._load_existing_records()
            self._enforce_retention()
        except BaseException:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            self._release_runtime_lease()
            raise

    @staticmethod
    def _check_confined_dir(dir_path: Path) -> Path:
        """Ensure the directory exists, is not a symlink at any level, and has mode 0700."""
        if dir_path.is_symlink():
            raise ValueError(f"Storage directory must not be a symlink: {dir_path}")
        dir_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if dir_path.is_symlink():
            raise ValueError(f"Storage directory must not be a symlink: {dir_path}")
        st = os.lstat(str(dir_path))
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise ValueError(f"Storage path is not a regular directory: {dir_path}")
        try:
            os.chmod(dir_path, 0o700)
        except OSError:
            pass
        return dir_path

    def _acquire_runtime_lease(self) -> None:
        if fcntl is None:
            raise RuntimeError("delegate-agy runtime leases require fcntl")
        path = self._runtime_lease_path(self.runtime_id)
        open_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        fd = os.open(path, open_flags, 0o600)
        local_fd = fd
        try:
            os.fchmod(local_fd, 0o600)
            fcntl.flock(local_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = json.dumps(
                {
                    "runtime_id": self.runtime_id,
                    "supervisor_pid": self.supervisor_pid,
                    "supervisor_started_at": self.supervisor_started_at,
                },
                allow_nan=False,
            ).encode("utf-8")
            os.ftruncate(local_fd, 0)
            os.write(local_fd, metadata)
            os.fsync(local_fd)
            with self._lease_lock:
                self._runtime_lease_fd = local_fd
                local_fd = None
        finally:
            if local_fd is not None:
                try:
                    fcntl.flock(local_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(local_fd)
                except OSError:
                    pass

    def _release_runtime_lease(self) -> None:
        with self._lease_lock:
            fd = self._runtime_lease_fd
            if fd is None:
                return
            self._runtime_lease_fd = None
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                path = self._runtime_lease_path(self.runtime_id)
                if not path.is_symlink():
                    path.unlink(missing_ok=True)
                    _fsync_dir(self.runtime_dir)
            except (OSError, ValueError):
                pass

    def _runtime_lease_status(self, runtime_id: str) -> str:
        """Tri-state probe for runtime lease: LOCKED, UNLOCKED, or UNKNOWN."""
        if fcntl is None:
            return LEASE_UNKNOWN
        try:
            path = self._runtime_lease_path(runtime_id)
        except ValueError:
            return LEASE_UNKNOWN
        if path.is_symlink():
            return LEASE_UNKNOWN

        open_flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, open_flags)
        except FileNotFoundError:
            try:
                if not path.exists() and not path.is_symlink():
                    return LEASE_UNLOCKED
            except OSError:
                pass
            return LEASE_UNKNOWN
        except (PermissionError, OSError):
            return LEASE_UNKNOWN

        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {11, 35}:
                    return LEASE_LOCKED
                return LEASE_UNKNOWN
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return LEASE_UNLOCKED
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _runtime_lease_is_locked(self, runtime_id: str) -> bool:
        return self._runtime_lease_status(runtime_id) == LEASE_LOCKED

    def _runtime_lease_path(self, runtime_id: str) -> Path:
        if not isinstance(runtime_id, str) or not _RUNTIME_ID_RE.fullmatch(runtime_id):
            raise ValueError(f"Invalid runtime_id format: {runtime_id!r}")
        path = (self.runtime_dir / f"{runtime_id}.lock").resolve()
        if not path.is_relative_to(self.runtime_dir.resolve()):
            raise ValueError(f"runtime_id path traversal detected: {runtime_id!r}")
        return path

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _setting_bool(value: Any, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "1", "on"}:
                return True
            if normalized in {"false", "no", "0", "off"}:
                return False
        return default

    def _gc_stale_runtime_locks(self) -> None:
        """Garbage-collect unlocked runtime leases left by cleanly or uncleanly exited runtimes."""
        if fcntl is None or not self.runtime_dir.exists():
            return
        try:
            candidates = list(self.runtime_dir.glob("*.lock"))
        except OSError:
            return
        for lock_path in candidates:
            if lock_path.is_symlink() or lock_path.name == f"{self.runtime_id}.lock":
                continue
            open_flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            try:
                fd = os.open(lock_path, open_flags)
            except OSError:
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    continue
                try:
                    lock_path.unlink(missing_ok=True)
                    _fsync_dir(self.runtime_dir)
                except OSError:
                    pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _gc_stale_temporary_files(self) -> None:
        """Clean up orphaned .tmp files older than 300 seconds."""
        now = time.time()
        for directory in (self.runs_dir, self.runtime_dir):
            if not directory.exists():
                continue
            try:
                for tmp_path in directory.glob("*.tmp"):
                    if tmp_path.is_symlink():
                        continue
                    try:
                        st = os.lstat(str(tmp_path))
                        if now - st.st_mtime > 300:
                            tmp_path.unlink(missing_ok=True)
                            _fsync_dir(directory)
                    except OSError:
                        pass
            except OSError:
                pass

    def _load_existing_records(self) -> None:
        paths = sorted(self.runs_dir.glob("agy_*.json"), key=self._path_mtime)
        for path in paths:
            record = self._validated_record_from_path(path)
            if record is None:
                continue
            if record.status not in TERMINAL_STATUSES:
                if record.runtime_id:
                    lease_status = self._runtime_lease_status(record.runtime_id)
                    if lease_status in {LEASE_LOCKED, LEASE_UNKNOWN}:
                        continue
                record.status = "interrupted"
                record.error = "Hermes/plugin runtime ended before this worker completed."
                record.finished_at = _now()
                self._write_record(record)
            self._records[record.worker_id] = record
        with self._lock:
            self._prune_records_locked()

    @staticmethod
    def _path_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _prune_records_locked(self) -> None:
        terminal = [
            record for record in self._records.values() if record.status in TERMINAL_STATUSES
        ]
        terminal.sort(key=lambda item: item.finished_at or item.created_at, reverse=True)
        keep_terminal = {
            record.worker_id for record in terminal[: self.max_records_in_memory]
        }
        for worker_id, record in list(self._records.items()):
            if record.status in TERMINAL_STATUSES and worker_id not in keep_terminal:
                self._records.pop(worker_id, None)

    def _read_record(self, worker_id: str) -> RunRecord | None:
        if not isinstance(worker_id, str) or not _WORKER_ID_RE.fullmatch(worker_id):
            return None
        try:
            path = self._record_path(worker_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        record = self._validated_record_from_path(path)
        if record is None or record.worker_id != worker_id:
            return None
        if record.status not in TERMINAL_STATUSES:
            if record.runtime_id:
                lease_status = self._runtime_lease_status(record.runtime_id)
                if lease_status == LEASE_LOCKED:
                    raise ValueError(
                        f"Worker is externally supervised by runtime {record.runtime_id}: {worker_id}"
                    )
                if lease_status == LEASE_UNKNOWN:
                    raise ValueError(
                        f"Worker runtime lease status could not be verified: {worker_id}"
                    )
            record.status = "interrupted"
            record.error = "Hermes/plugin runtime ended before this worker completed."
            record.finished_at = _now()
            self._write_record(record)
        return record

    @staticmethod
    def _sanitize_usage(usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            return {}
        sanitized: dict[str, Any] = {}
        for k, v in usage.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
                sanitized[str(k)] = v
        return sanitized

    def _validated_record_from_path(self, path: Path) -> RunRecord | None:
        """Load one confined, schema-validated record without rewriting it."""
        try:
            runs_root = self.runs_dir.resolve()
            if path.is_symlink():
                return None
            st = os.lstat(str(path))
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > 2 * 1024 * 1024:
                return None
            resolved = path.resolve()
            if not resolved.is_relative_to(runs_root) or resolved.parent != runs_root:
                return None
            worker_id_from_filename = resolved.stem
            if not _WORKER_ID_RE.fullmatch(worker_id_from_filename):
                return None

            def _reject_constants(val: str):
                raise ValueError(f"Invalid non-finite constant: {val}")

            raw_text = resolved.read_text(encoding="utf-8", errors="replace")
            decoder = json.JSONDecoder(parse_constant=_reject_constants)
            raw, _ = decoder.raw_decode(raw_text)
            if not isinstance(raw, dict):
                return None

            worker_id = raw.get("worker_id")
            if not isinstance(worker_id, str) or worker_id != worker_id_from_filename or not _WORKER_ID_RE.fullmatch(worker_id):
                return None
            owner_id = raw.get("owner_id")
            if not isinstance(owner_id, str) or not owner_id.strip() or owner_id.strip().lower() == "none":
                return None
            owner_id = owner_id.strip()
            status = raw.get("status")
            if not isinstance(status, str) or status not in ALL_KNOWN_STATUSES:
                return None
            goal = raw.get("goal")
            if not isinstance(goal, str):
                return None
            context = raw.get("context", "")
            if not isinstance(context, str):
                return None
            prompt = raw.get("prompt", "")
            if not isinstance(prompt, str):
                return None
            task_type = raw.get("task_type", "investigation")
            if not isinstance(task_type, str) or task_type not in MODEL_BY_TASK_TYPE:
                return None
            model = raw.get("model", "")
            if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
                return None
            execution_mode = raw.get("execution_mode", "sandbox")
            if not isinstance(execution_mode, str) or execution_mode not in {"sandbox", "accept_edits"}:
                return None
            effort = raw.get("effort")
            if effort is not None and (not isinstance(effort, str) or effort not in {"low", "medium", "high"}):
                return None
            workdir = raw.get("workdir", "")
            if not isinstance(workdir, str):
                return None
            timeout_seconds = raw.get("timeout_seconds")
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or int(timeout_seconds) < 1:
                return None
            timeout_seconds = int(timeout_seconds)

            runtime_id = raw.get("runtime_id")
            if runtime_id is not None and (not isinstance(runtime_id, str) or not _RUNTIME_ID_RE.fullmatch(runtime_id)):
                return None

            raw_response = str(raw.get("response", ""))
            response_trunc = bool(raw.get("response_truncated", False)) or len(raw_response) > self.max_result_chars
            bounded_response, _ = _truncate_utf8(raw_response, self.max_result_chars)

            record = RunRecord(
                worker_id=worker_id,
                owner_id=owner_id,
                goal=goal,
                context=context,
                prompt=prompt,
                task_type=task_type,
                model=model,
                execution_mode=execution_mode,
                effort=effort,
                workdir=workdir,
                timeout_seconds=timeout_seconds,
                status=status,
                created_at=str(raw.get("created_at") or _now()),
                started_at=str(raw["started_at"]) if raw.get("started_at") is not None else None,
                finished_at=str(raw["finished_at"]) if raw.get("finished_at") is not None else None,
                pid=int(raw["pid"]) if isinstance(raw.get("pid"), int) and not isinstance(raw.get("pid"), bool) else None,
                pgid=int(raw["pgid"]) if isinstance(raw.get("pgid"), int) and not isinstance(raw.get("pgid"), bool) else None,
                returncode=int(raw["returncode"]) if isinstance(raw.get("returncode"), int) and not isinstance(raw.get("returncode"), bool) else None,
                conversation_id=str(raw["conversation_id"]) if raw.get("conversation_id") is not None else None,
                parent_worker_id=str(raw["parent_worker_id"]) if raw.get("parent_worker_id") is not None else None,
                response=bounded_response,
                response_truncated=response_trunc,
                usage=self._sanitize_usage(raw.get("usage")),
                duration_seconds=float(raw["duration_seconds"]) if self._finite_number(raw.get("duration_seconds")) else None,
                num_turns=int(raw["num_turns"]) if self._finite_number(raw.get("num_turns")) else None,
                error=str(raw["error"]) if raw.get("error") is not None else None,
                stdout_path=str(raw["stdout_path"]) if raw.get("stdout_path") is not None else None,
                stderr_path=str(raw["stderr_path"]) if raw.get("stderr_path") is not None else None,
                steer_queue=[str(s) for s in raw["steer_queue"] if isinstance(s, str)] if isinstance(raw.get("steer_queue"), list) else [],
                cancel_requested=bool(raw.get("cancel_requested", False)),
                runtime_id=runtime_id,
                supervisor_pid=int(raw["supervisor_pid"]) if isinstance(raw.get("supervisor_pid"), int) and not isinstance(raw.get("supervisor_pid"), bool) else None,
                supervisor_started_at=str(raw["supervisor_started_at"]) if raw.get("supervisor_started_at") is not None else None,
                attempt_index=int(raw["attempt_index"]) if isinstance(raw.get("attempt_index"), int) and not isinstance(raw.get("attempt_index"), bool) and int(raw.get("attempt_index", 1)) >= 1 else 1,
            )
            return record
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _record_path(self, worker_id: str) -> Path:
        if not isinstance(worker_id, str) or not _WORKER_ID_RE.fullmatch(worker_id):
            raise ValueError(f"Invalid worker_id format: {worker_id!r}")
        path = (self.runs_dir / f"{worker_id}.json").resolve()
        if not path.is_relative_to(self.runs_dir.resolve()):
            raise ValueError(f"worker_id path traversal detected: {worker_id!r}")
        return path

    def _write_record(self, record: RunRecord) -> None:
        path = self._record_path(record.worker_id)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        try:
            fd = os.open(temporary, open_flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    asdict(record),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                    default=str,
                )
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o600)
            os.replace(temporary, path)
            _fsync_dir(self.runs_dir)
        finally:
            try:
                if temporary.exists():
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _persist(self, record: RunRecord) -> None:
        with self._lock:
            self._records[record.worker_id] = record
            self._write_record(record)

    def _transition(
        self,
        record: RunRecord,
        status: str,
        *,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        """Apply and persist one record transition while the manager lock is held."""
        record.status = status
        record.error = error
        if finished:
            record.finished_at = _now()
        self._records[record.worker_id] = record
        self._write_record(record)

    @staticmethod
    def _owner_id(kwargs: dict[str, Any]) -> str:
        session_id = kwargs.get("session_id")
        if session_id is not None:
            session_id_str = str(session_id).strip()
            if session_id_str:
                return session_id_str
        task_id = kwargs.get("task_id")
        if task_id is not None:
            task_id_str = str(task_id).strip()
            if task_id_str:
                return task_id_str
        raise ValueError("Hermes did not provide a valid task_id or session_id; refusing unscoped access")

    def _owned_record(self, owner_id: str, worker_id: str) -> RunRecord:
        with self._lock:
            record = self._records.get(worker_id)
            if record is None:
                record = self._record_cache.get(worker_id)
                if record is not None:
                    self._record_cache.move_to_end(worker_id)
        if record is None:
            record = self._read_record(worker_id)
            if record is not None:
                with self._lock:
                    self._record_cache[worker_id] = record
                    self._record_cache.move_to_end(worker_id)
                    while len(self._record_cache) > self._record_cache_limit:
                        self._record_cache.popitem(last=False)
        if record is None or record.owner_id != owner_id:
            raise ValueError(f"Worker not found in this session: {worker_id}")
        return record

    def _summary(self, record: RunRecord) -> dict[str, Any]:
        return {
            "worker_id": record.worker_id,
            "status": record.status,
            "task_type": record.task_type,
            "model": record.model,
            "execution_mode": record.execution_mode,
            "goal": record.goal[:300],
            "workdir": record.workdir,
            "conversation_id": record.conversation_id,
            "parent_worker_id": record.parent_worker_id,
            "pid": record.pid,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "error": record.error,
        }

    def _result(self, record: RunRecord) -> dict[str, Any]:
        response = record.response
        truncated = record.response_truncated or len(response) > self.max_result_chars
        if len(response) > self.max_result_chars:
            response = response[: self.max_result_chars] + "\n[truncated; inspect stdout_path]"
        elif truncated and not response.endswith("[truncated; inspect stdout_path]"):
            response = response + "\n[truncated; inspect stdout_path]"

        result = self._summary(record)
        result.update(
            {
                "response": response,
                "response_truncated": truncated,
                "usage": record.usage,
                "duration_seconds": record.duration_seconds,
                "num_turns": record.num_turns,
                "returncode": record.returncode,
                "stdout_path": record.stdout_path,
                "stderr_path": record.stderr_path,
                "queued_steers": len(record.steer_queue),
                "attempt_index": record.attempt_index,
            }
        )
        return result

    @staticmethod
    def _validate_text(
        value: Any,
        field_name: str,
        *,
        required: bool = False,
        max_len: int = 100000,
    ) -> str:
        text = "" if value is None else str(value).strip()
        if required and not text:
            raise ValueError(f"{field_name} is required")
        if len(text) > max_len:
            raise ValueError(f"{field_name} exceeds {max_len} characters")
        return text

    def _task_spec(self, raw: dict[str, Any], default_workdir: str | None) -> dict[str, Any]:
        goal = self._validate_text(raw.get("goal"), "goal", required=True, max_len=64000)
        context = self._validate_text(raw.get("context"), "context", max_len=64000)
        task_type = str(raw.get("task_type") or "investigation")
        if task_type not in MODEL_BY_TASK_TYPE:
            raise ValueError(f"Unknown task_type: {task_type}")
        explicit_model = self._validate_text(raw.get("model"), "model", max_len=128)
        model = explicit_model or MODEL_BY_TASK_TYPE[task_type]
        if not _MODEL_RE.fullmatch(model):
            raise ValueError(f"Invalid agy model ID: {model}")
        execution_mode = str(raw.get("execution_mode") or "sandbox")
        if execution_mode not in {"sandbox", "accept_edits"}:
            raise ValueError(f"Unknown execution_mode: {execution_mode}")
        if execution_mode == "accept_edits" and not self.allow_accept_edits:
            raise ValueError("execution_mode=accept_edits is disabled by plugin settings")
        effort_value = raw.get("effort")
        effort = str(effort_value) if effort_value is not None else None
        if effort not in {None, "low", "medium", "high"}:
            raise ValueError(f"Unknown effort: {effort}")
        requested_workdir = raw.get("workdir") or default_workdir or os.getcwd()
        workdir_path = Path(str(requested_workdir)).expanduser().resolve()
        if not workdir_path.is_dir():
            raise ValueError(f"workdir is not an existing directory: {workdir_path}")
        timeout_seconds = self._bounded_int(
            raw.get("timeout_seconds", self.default_timeout),
            default=self.default_timeout,
            minimum=30,
            maximum=7200,
        )
        prompt = self._worker_prompt(goal, context, str(workdir_path))
        if len(prompt.encode("utf-8")) > 64000:
            raise ValueError("Combined task goal and context exceeds maximum size of 64KB")
        return {
            "goal": goal,
            "context": context,
            "prompt": prompt,
            "task_type": task_type,
            "model": model,
            "execution_mode": execution_mode,
            "effort": effort,
            "workdir": str(workdir_path),
            "timeout_seconds": timeout_seconds,
        }

    @staticmethod
    def _worker_prompt(goal: str, context: str, workdir: str) -> str:
        sections = [
            "You are an Antigravity worker delegated by a Hermes main agent.",
            "Complete the task autonomously and verify factual or code claims with real tools.",
            "Do not commit or push unless the task explicitly asks for it.",
            (
                f"Your assigned workspace root is exactly: {workdir}\n"
                "Resolve every relative path under that directory. Do not write task files "
                "to the Antigravity scratch directory."
            ),
            f"\nTASK\n{goal}",
        ]
        if context:
            sections.append(f"\nCONTEXT AND CONSTRAINTS\n{context}")
        sections.append(
            "\nRETURN\nGive a concise result with files changed, commands run, observed output, "
            "and any remaining blocker. Do not claim success without verification."
        )
        return "\n".join(sections)

    def _future_done(self, worker_id: str, completed: Future[None]) -> None:
        with self._lock:
            if self._futures.get(worker_id) is completed:
                self._futures.pop(worker_id, None)

    def _supervise_worker(self, worker_id: str) -> None:
        try:
            self._run_worker(worker_id)
        except BaseException as exc:
            with self._lock:
                record = self._records.get(worker_id)
                if record is not None and record.status not in TERMINAL_STATUSES:
                    error = f"Worker supervisor failed: {exc}"
                    try:
                        self._transition(record, "failed", error=error, finished=True)
                    except OSError:
                        record.status = "failed"
                        record.error = error
                        record.finished_at = record.finished_at or _now()
        finally:
            with self._lock:
                self._prune_records_locked()
            self._enforce_retention()

    def _build_command(self, record: RunRecord, prompt: str) -> list[str]:
        command = [
            self.agy_path,
            "-p",
            prompt,
            "--add-dir",
            record.workdir,
            "--model",
            record.model,
            "--output-format",
            "json",
            "--print-timeout",
            f"{record.timeout_seconds}s",
        ]
        if record.conversation_id:
            command.extend(["--conversation", record.conversation_id])
        if record.effort:
            command.extend(["--effort", record.effort])
        if record.execution_mode == "accept_edits":
            command.extend(["--mode=accept-edits", "--dangerously-skip-permissions"])
        else:
            command.extend(["--mode=plan", "--sandbox"])
        return command

    @staticmethod
    def _build_sanitized_env(agy_path: str | None = None) -> dict[str, str]:
        """Build the subprocess env, choosing allowlist or denylist based
        on whether the agy binary is a native executable or a shim script.

        Backwards-compatible: callers that do not know the path (legacy
        probes, dry-runs) get the strict allowlist, which is the safer
        fallback. The hot path (``_execute_once``) always passes the
        real path so the script branch is selected when appropriate.
        """
        if not agy_path:
            return _build_env_allowlist()
        return _build_env_for_agy(agy_path)

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any] | None:
        cleaned = stdout.strip()
        if not cleaned:
            return None

        forbidden_keys = {
            "event",
            "level",
            "logger",
            "timestamp",
            "log",
            "message_type",
            "log_level",
            "caller",
        }

        def is_result_payload(candidate: Any) -> bool:
            if not isinstance(candidate, dict):
                return False
            # Veto log markers FIRST before evaluating status
            if any(k in candidate for k in forbidden_keys):
                return False

            has_response = isinstance(candidate.get("response"), str)
            has_conv_id = isinstance(candidate.get("conversation_id"), str) and bool(candidate.get("conversation_id"))
            has_error = candidate.get("error") is not None and isinstance(candidate.get("error"), str)
            status = candidate.get("status")
            status_str = str(status).upper() if isinstance(status, str) else None

            if status_str in {"SUCCESS", "SUCCEEDED", "DONE", "COMPLETED"}:
                return has_response or has_conv_id
            if status_str in {"ERROR", "FAILED"}:
                return has_error
            if status is None:
                return has_response or has_error or has_conv_id
            return False

        def _reject_constants(val: str):
            raise ValueError(f"Invalid non-finite JSON constant: {val}")

        decoder = json.JSONDecoder(parse_constant=_reject_constants)

        try:
            payload, _ = decoder.raw_decode(cleaned)
            if is_result_payload(payload):
                return payload
        except (json.JSONDecodeError, ValueError):
            pass

        candidates: list[dict[str, Any]] = []
        cursor = 0
        while cursor < len(cleaned):
            object_start = cleaned.find("{", cursor)
            array_start = cleaned.find("[", cursor)
            starts = [index for index in (object_start, array_start) if index >= 0]
            if not starts:
                break
            start = min(starts)
            try:
                candidate, end = decoder.raw_decode(cleaned, start)
            except (json.JSONDecodeError, ValueError):
                cursor = start + 1
                continue
            if is_result_payload(candidate):
                candidates.append(candidate)
            cursor = max(end, start + 1)
        return candidates[-1] if candidates else None

    @staticmethod
    def _finite_number(value: Any) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    def _interpret_payload(
        self,
        record: RunRecord,
        payload: dict[str, Any] | None,
        *,
        returncode: int | None,
        stdout: str,
        stderr: str,
    ) -> bool:
        agy_status = ""
        if payload is not None:
            conversation_id = payload.get("conversation_id")
            if isinstance(conversation_id, str) and conversation_id:
                record.conversation_id = conversation_id
            if "response" in payload and isinstance(payload.get("response"), str):
                resp_text, truncated = _truncate_utf8(payload["response"], self.max_result_chars)
                record.response = resp_text
                record.response_truncated = truncated or bool(payload.get("response_truncated", False))
            usage = payload.get("usage")
            record.usage = self._sanitize_usage(usage)
            duration = payload.get("duration_seconds")
            record.duration_seconds = (
                float(duration) if self._finite_number(duration) else None
            )
            turns = payload.get("num_turns")
            record.num_turns = int(turns) if self._finite_number(turns) else None
            agy_status = str(payload.get("status") or "").upper()

        successful = (
            returncode == 0
            and payload is not None
            and agy_status in {"SUCCESS", "SUCCEEDED", "DONE", "COMPLETED"}
        )
        if successful:
            record.error = None
            return True

        structured_error = payload.get("error") if payload is not None and isinstance(payload.get("error"), str) else None
        details = (
            str(structured_error).strip()
            if structured_error is not None and str(structured_error).strip()
            else stderr.strip()
            or stdout.strip()
            or "agy returned no structured output"
        )
        record.error = (
            f"agy failed with return code {returncode}"
            f" and status {agy_status or 'unknown'}: {details[:2000]}"
        )
        return False

    @staticmethod
    def _captured_group_is_owned(
        process: subprocess.Popen[str], pgid: int | None
    ) -> bool:
        return (
            os.name == "posix"
            and isinstance(pgid, int)
            and pgid > 0
            and pgid == process.pid
            and pgid != os.getpgrp()
        )

    @staticmethod
    def _group_exists(pgid: int | None) -> bool:
        if not isinstance(pgid, int) or pgid <= 0:
            return False
        if os.name == "posix":
            try:
                if pgid == os.getpgrp():
                    return False
            except OSError:
                pass
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _close_process_pipes(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdout, process.stderr, process.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _terminate_process(
        self, process: subprocess.Popen[str], pgid: int | None
    ) -> bool:
        """Boundedly terminate owned work and report whether ownership is quiescent."""
        if os.name == "posix" and self._captured_group_is_owned(process, pgid):
            assert pgid is not None
            if self._group_exists(pgid):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                deadline = time.monotonic() + PROCESS_GROUP_TERM_GRACE_SECONDS
                while time.monotonic() < deadline and self._group_exists(pgid):
                    time.sleep(0.02)
                if self._group_exists(pgid):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
        else:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=PROCESS_GROUP_TERM_GRACE_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass

        try:
            process.wait(timeout=PROCESS_REAP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass

        if os.name == "posix" and self._captured_group_is_owned(process, pgid):
            assert pgid is not None
            deadline = time.monotonic() + PROCESS_REAP_TIMEOUT_SECONDS
            while time.monotonic() < deadline and self._group_exists(pgid):
                time.sleep(0.02)
            return not self._group_exists(pgid)
        return process.poll() is not None

    def _drop_process_if_quiescent(
        self,
        worker_id: str,
        ownership: OwnedProcess,
        *,
        terminate: bool,
    ) -> bool:
        process = ownership.process
        if terminate:
            quiescent = self._terminate_process(process, ownership.pgid)
        elif os.name == "posix" and self._captured_group_is_owned(process, ownership.pgid):
            quiescent = not self._group_exists(ownership.pgid)
        else:
            quiescent = process.poll() is not None
        if not quiescent:
            return False
        self._close_process_pipes(process)
        with self._lock:
            if self._processes.get(worker_id) is ownership:
                self._processes.pop(worker_id, None)
            self._reap_worker_ids.discard(worker_id)
        return True

    def _ensure_resource_reaper(self, *worker_ids: str) -> None:
        """Keep process ownership and the runtime lease until all work is quiescent."""
        with self._lock:
            # Only track worker IDs that actually have active processes
            valid_ids = [wid for wid in worker_ids if wid in self._processes]
            self._reap_worker_ids.update(valid_ids)
            if not self._reap_worker_ids and not self._closed:
                return
        with self._lease_lock:
            existing = self._reaper_thread
            if existing is not None and existing.is_alive():
                return
            reaper = threading.Thread(
                target=self._reap_resources,
                name="delegate-agy-resource-reaper",
                daemon=True,
            )
            self._reaper_thread = reaper
            reaper.start()

    def _reap_resources(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._lock:
                    closed = self._closed
                    self._reap_worker_ids.intersection_update(self._processes.keys())
                    if closed:
                        owned_processes = list(self._processes.items())
                    else:
                        owned_processes = [
                            (worker_id, ownership)
                            for worker_id, ownership in self._processes.items()
                            if worker_id in self._reap_worker_ids
                        ]

                for worker_id, ownership in owned_processes:
                    self._drop_process_if_quiescent(
                        worker_id,
                        ownership,
                        terminate=True,
                    )

                with self._lock:
                    self._reap_worker_ids.intersection_update(self._processes.keys())
                    processes_remain = bool(self._processes)
                    closed = self._closed
                    pending_cleanup = bool(self._reap_worker_ids)
                    live_futures = (
                        [future for future in self._futures.values() if not future.done()]
                        if closed
                        else []
                    )

                if closed and not processes_remain and not live_futures:
                    self._release_runtime_lease()
                    return
                if not closed and not pending_cleanup:
                    return

                for future in live_futures:
                    try:
                        future.result(timeout=0.05)
                    except FutureTimeout:
                        pass
                    except BaseException:
                        pass
                time.sleep(0.02)
        finally:
            cleared_current = False
            with self._lease_lock:
                if self._reaper_thread is current:
                    self._reaper_thread = None
                    cleared_current = True
            if cleared_current:
                with self._lock:
                    closed = self._closed
                    self._reap_worker_ids.intersection_update(self._processes.keys())
                    resources_remain = bool(self._processes) or any(
                        not future.done() for future in self._futures.values()
                    )
                if closed and resources_remain:
                    self._ensure_resource_reaper()
                elif closed:
                    self._release_runtime_lease()

    def _drain_and_stream(
        self,
        process: subprocess.Popen[str],
        stdout_dest: Path,
        stderr_dest: Path,
        timeout: float,
    ) -> tuple[str, str, str | None]:
        """Stream process output directly to disk up to max_raw_output_bytes and sample memory."""
        stdout_temp = stdout_dest.with_name(f"{stdout_dest.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        stderr_temp = stderr_dest.with_name(f"{stderr_dest.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        stdout_sample = bytearray()
        stderr_sample = bytearray()

        def _reader(stream, temp_path, sample_buf):
            open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= os.O_NOFOLLOW
            written = 0
            fd = None
            try:
                fd = os.open(temp_path, open_flags, 0o600)
                while True:
                    try:
                        chunk = stream.read(16384)
                    except (ValueError, OSError):
                        break
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk_bytes = chunk.encode("utf-8", errors="replace")
                    else:
                        chunk_bytes = bytes(chunk)
                    if len(sample_buf) < MAX_SAMPLE_MEMORY_BYTES:
                        avail = MAX_SAMPLE_MEMORY_BYTES - len(sample_buf)
                        sample_buf.extend(chunk_bytes[:avail])
                    if written < self.max_raw_output_bytes:
                        to_write = chunk_bytes[: self.max_raw_output_bytes - written]
                        os.write(fd, to_write)
                        written += len(to_write)
                if fd is not None:
                    os.fsync(fd)
                    os.fchmod(fd, 0o600)
            except OSError:
                pass
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        threads = []
        if process.stdout is not None:
            t_out = threading.Thread(
                target=_reader,
                args=(process.stdout, stdout_temp, stdout_sample),
                daemon=True,
            )
            threads.append(t_out)
            t_out.start()
        if process.stderr is not None:
            t_err = threading.Thread(
                target=_reader,
                args=(process.stderr, stderr_temp, stderr_sample),
                daemon=True,
            )
            threads.append(t_err)
            t_err.start()

        drain_error = None
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            drain_error = f"agy exceeded the timeout of {timeout}s"
        except Exception as exc:
            drain_error = f"Process wait failed: {exc}"

        for t in threads:
            t.join(timeout=PIPE_DRAIN_TIMEOUT_SECONDS)
        self._close_process_pipes(process)

        try:
            if stdout_temp.exists():
                os.replace(stdout_temp, stdout_dest)
            if stderr_temp.exists():
                os.replace(stderr_temp, stderr_dest)
            _fsync_dir(self.runs_dir)
        except OSError:
            pass
        finally:
            try:
                if stdout_temp.exists():
                    stdout_temp.unlink(missing_ok=True)
                if stderr_temp.exists():
                    stderr_temp.unlink(missing_ok=True)
            except OSError:
                pass

        stdout_str = stdout_sample.decode("utf-8", errors="replace")
        stderr_str = stderr_sample.decode("utf-8", errors="replace")
        return stdout_str, stderr_str, drain_error

    def _execute_once(self, record: RunRecord, prompt: str) -> bool:
        command = self._build_command(record, prompt)
        creation: dict[str, Any] = {}
        if os.name == "posix":
            creation["start_new_session"] = True
        process: subprocess.Popen[str]
        pgid: int | None = None
        attempt = max(1, record.attempt_index)
        stdout_dest = self.runs_dir / f"{record.worker_id}.turn-{attempt:03d}.stdout"
        stderr_dest = self.runs_dir / f"{record.worker_id}.turn-{attempt:03d}.stderr"

        try:
            process = subprocess.Popen(
                command,
                cwd=record.workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                env=self._build_sanitized_env(self.agy_path),
                **creation,
            )
        except OSError as exc:
            with self._lock:
                record.error = f"Could not start agy: {exc}"
                record.returncode = None
                try:
                    self._write_record(record)
                except OSError:
                    pass
            return False

        try:
            if os.name == "posix":
                pgid = process.pid
                # Bounded verification of PGID
                for _ in range(5):
                    try:
                        actual_pgid = os.getpgid(process.pid)
                        if actual_pgid == process.pid:
                            pgid = actual_pgid
                            break
                    except (ProcessLookupError, OSError):
                        pass
                    time.sleep(0.01)

            with self._lock:
                ownership = OwnedProcess(process, pgid)
                self._processes[record.worker_id] = ownership
                record.pid = process.pid
                record.pgid = pgid
                record.stdout_path = str(stdout_dest)
                record.stderr_path = str(stderr_dest)
                try:
                    self._write_record(record)
                except OSError as exc:
                    raise RuntimeError(f"Post-spawn persistence failed: {exc}") from exc
                cancel_requested = record.cancel_requested

            if cancel_requested:
                self._terminate_process(process, pgid)

            timeout = record.timeout_seconds + 30
            stdout, stderr, drain_error = self._drain_and_stream(
                process, stdout_dest, stderr_dest, timeout
            )

            if cancel_requested:
                with self._lock:
                    record.returncode = process.returncode
                    try:
                        self._transition(record, "cancelled", error="Cancelled by user or session.", finished=True)
                    except OSError:
                        pass
                return False

            if drain_error:
                self._terminate_process(process, pgid)
                status = "timed_out" if "timeout" in drain_error else "failed"
                with self._lock:
                    record.returncode = process.returncode
                    try:
                        self._transition(record, status, error=drain_error, finished=True)
                    except OSError:
                        record.status = status
                        record.error = drain_error
                        record.finished_at = record.finished_at or _now()
                return False

            if os.name == "posix" and pgid is not None and self._group_exists(pgid):
                self._terminate_process(process, pgid)

            payload = self._parse_payload(stdout)
            if payload is None and stdout_dest.exists():
                try:
                    file_size = stdout_dest.stat().st_size
                    if 0 < file_size <= 4 * 1024 * 1024:
                        raw_file_text = stdout_dest.read_text(encoding="utf-8", errors="replace")
                        payload = self._parse_payload(raw_file_text)
                except OSError:
                    pass

            with self._lock:
                record.returncode = process.returncode
                successful = self._interpret_payload(
                    record,
                    payload,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
                self._write_record(record)
            return successful
        except BaseException as exc:
            self._terminate_process(process, pgid)
            self._close_process_pipes(process)
            with self._lock:
                record.returncode = process.returncode
                try:
                    self._transition(record, "failed", error=str(exc), finished=True)
                except OSError:
                    record.status = "failed"
                    record.error = str(exc)
                    record.finished_at = record.finished_at or _now()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return False
        finally:
            ownership = OwnedProcess(process, pgid)
            with self._lock:
                published = self._processes.get(record.worker_id)
                if published is not None and published.process is process:
                    ownership = published
            quiescent = self._drop_process_if_quiescent(
                record.worker_id,
                ownership,
                terminate=True,
            )
            if not quiescent:
                self._close_process_pipes(process)
                self._ensure_resource_reaper(record.worker_id)
                raise RuntimeError(
                    f"Process group cleanup was not confirmed for worker {record.worker_id} "
                    f"(pgid={pgid})"
                )

    def _record_retention_error(self, exc: BaseException) -> None:
        with self._lock:
            self._retention_errors.append(str(exc))
            del self._retention_errors[:-20]

    def _delete_terminal_bundle(self, worker_id: str) -> None:
        if not isinstance(worker_id, str) or not _WORKER_ID_RE.fullmatch(worker_id):
            return
        for path in self._bundle_paths(worker_id):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._record_retention_error(exc)
        _fsync_dir(self.runs_dir)
        with self._lock:
            record = self._records.get(worker_id)
            if record is not None and record.status in TERMINAL_STATUSES:
                self._records.pop(worker_id, None)
            self._record_cache.pop(worker_id, None)

    def _bundle_paths(self, worker_id: str) -> list[Path]:
        if not isinstance(worker_id, str) or not _WORKER_ID_RE.fullmatch(worker_id):
            return []
        bundle_name = re.compile(
            rf"\A{re.escape(worker_id)}(?:\.json|\.stdout|\.stderr|"
            rf"\.turn-[0-9]{{3,}}\.(?:stdout|stderr))\Z"
        )
        runs_root = self.runs_dir.resolve()
        paths: list[Path] = []
        try:
            candidates = list(self.runs_dir.iterdir())
        except OSError:
            return []
        for candidate in candidates:
            if not bundle_name.fullmatch(candidate.name) or candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.parent == runs_root and resolved.is_relative_to(runs_root):
                paths.append(resolved)
        return paths

    def _terminal_bundles(self) -> list[tuple[float, str, list[Path]]]:
        bundles: list[tuple[float, str, list[Path]]] = []
        for record_path in self.runs_dir.glob("agy_*.json"):
            record = self._validated_record_from_path(record_path)
            if record is None:
                continue
            if record.status not in TERMINAL_STATUSES:
                continue
            paths = self._bundle_paths(record.worker_id)
            bundles.append((self._path_mtime(record_path), record.worker_id, paths))
        bundles.sort(key=lambda item: item[0])
        return bundles

    def _enforce_retention_once(self) -> None:
        self._gc_stale_runtime_locks()
        self._gc_stale_temporary_files()

        cutoff = time.time() - self.retention_days * 86400
        for modified, worker_id, _paths in self._terminal_bundles():
            if modified < cutoff:
                self._delete_terminal_bundle(worker_id)

        limit_bytes = self.max_total_storage_mb * 1024 * 1024
        try:
            files = [
                path
                for path in self.runs_dir.iterdir()
                if path.is_file() and not path.is_symlink()
            ]
            total = sum(path.stat().st_size for path in files)
        except OSError:
            return
        if total <= limit_bytes:
            return
        for _modified, worker_id, paths in self._terminal_bundles():
            bundle_size = sum(path.stat().st_size for path in paths if path.exists())
            self._delete_terminal_bundle(worker_id)
            total -= bundle_size
            if total <= limit_bytes:
                break

    def _enforce_retention(self) -> None:
        with self._retention_state_lock:
            if self._retention_running:
                self._retention_pending = True
                return
            self._retention_running = True
        try:
            while True:
                try:
                    self._enforce_retention_once()
                except (OSError, ValueError, TypeError) as exc:
                    self._record_retention_error(exc)
                with self._retention_state_lock:
                    if self._retention_pending:
                        self._retention_pending = False
                        continue
                    self._retention_running = False
                    return
        except BaseException:
            with self._retention_state_lock:
                self._retention_running = False
            raise

    def _run_worker(self, worker_id: str) -> None:
        with self._lock:
            record = self._records[worker_id]
            if record.cancel_requested:
                self._transition(
                    record,
                    "cancelled",
                    error="Cancelled before the worker process started.",
                    finished=True,
                )
                return
            record.status = "running"
            record.started_at = _now()
            prompt = record.prompt
            self._write_record(record)

        while True:
            successful = self._execute_once(record, prompt)
            with self._lock:
                if record.cancel_requested:
                    self._transition(
                        record,
                        "cancelled",
                        error=record.error or "Cancelled by the parent Hermes session.",
                        finished=True,
                    )
                    return
                if not successful:
                    status = "timed_out" if record.status == "timed_out" else "failed"
                    self._transition(
                        record,
                        status,
                        error=record.error,
                        finished=True,
                    )
                    return
                if record.steer_queue:
                    if not record.conversation_id:
                        self._transition(
                            record,
                            "failed",
                            error=(
                                "agy returned no conversation_id, so queued steering "
                                "cannot continue."
                            ),
                            finished=True,
                        )
                        return
                    prompt = record.steer_queue.pop(0)
                    record.attempt_index += 1
                    record.status = "continuing"
                    self._write_record(record)
                    continue
                self._transition(record, "succeeded", error=None, finished=True)
                return

    def _wait_for(self, records: list[RunRecord], timeout_seconds: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        for record in records:
            with self._lock:
                future = self._futures.get(record.worker_id)
            if future is None or future.done():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                future.result(timeout=remaining)
            except FutureTimeout:
                break
            except Exception as exc:
                with self._lock:
                    self._transition(
                        record,
                        "failed",
                        error=f"Worker supervisor failed: {exc}",
                        finished=True,
                    )
        with self._lock:
            return [self._result(record) for record in records]

    def _spawn(self, args: dict[str, Any], owner_id: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        tasks = args.get("tasks")
        if tasks is not None:
            if not isinstance(tasks, list) or not tasks:
                raise ValueError("tasks must be a non-empty array")
            if len(tasks) > 10:
                raise ValueError("At most 10 tasks may be spawned in one call")
            common_fields = (
                "context",
                "task_type",
                "model",
                "execution_mode",
                "effort",
                "workdir",
                "timeout_seconds",
            )
            common_defaults = {
                field_name: args[field_name]
                for field_name in common_fields
                if field_name in args
            }
            raw_specs = []
            for index, task in enumerate(tasks):
                if not isinstance(task, Mapping):
                    raise ValueError(f"tasks[{index}] must be a mapping")
                raw_specs.append({**common_defaults, **dict(task)})
        else:
            raw_specs = [args]

        default_workdir = kwargs.get("workdir") or kwargs.get("cwd")
        specs = [self._task_spec(dict(raw), default_workdir) for raw in raw_specs]

        records: list[RunRecord] = []
        with self._lock:
            if self._closed:
                raise RuntimeError("delegate-agy is shutting down")
            if self._executor is None:
                raise RuntimeError("delegate-agy executor is unavailable")

            # Check backpressure limits atomically
            active_global = sum(1 for r in self._records.values() if r.status not in TERMINAL_STATUSES)
            if active_global + len(specs) > self.max_pending_workers:
                raise ValueError(
                    f"Global worker capacity exceeded: maximum pending workers is {self.max_pending_workers}"
                )
            active_owner = sum(
                1 for r in self._records.values() if r.owner_id == owner_id and r.status not in TERMINAL_STATUSES
            )
            if active_owner + len(specs) > self.max_pending_per_owner:
                raise ValueError(
                    f"Session worker capacity exceeded: maximum pending workers per session is {self.max_pending_per_owner}"
                )

            # Transactional spawn: reserve all records and submit
            try:
                for spec in specs:
                    worker_id = f"agy_{uuid.uuid4().hex[:12]}"
                    record = RunRecord(
                        worker_id=worker_id,
                        owner_id=owner_id,
                        runtime_id=self.runtime_id,
                        supervisor_pid=self.supervisor_pid,
                        supervisor_started_at=self.supervisor_started_at,
                        **spec,
                    )
                    records.append(record)
                    try:
                        self._write_record(record)
                    except OSError as exc:
                        try:
                            self._record_path(record.worker_id).unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise RuntimeError(f"Initial persistence failed: {exc}") from exc
                    self._records[worker_id] = record

                for record in records:
                    future = self._executor.submit(self._supervise_worker, record.worker_id)
                    self._futures[record.worker_id] = future
                    future.add_done_callback(
                        lambda completed, target=record.worker_id: self._future_done(target, completed)
                    )
            except BaseException as exc:
                # Compensate already prepared workers
                for record in records:
                    future = self._futures.pop(record.worker_id, None)
                    if future is not None:
                        future.cancel()
                    self._records.pop(record.worker_id, None)
                    record.cancel_requested = True
                    ownership = self._processes.get(record.worker_id)
                    if ownership is not None:
                        self._drop_process_if_quiescent(record.worker_id, ownership, terminate=True)
                    record.status = "failed"
                    record.finished_at = _now()
                    record.error = f"Could not submit worker: {exc}"
                    try:
                        self._record_path(record.worker_id).unlink(missing_ok=True)
                    except OSError:
                        pass
                compensated_ids = [r.worker_id for r in records]
                raise RuntimeError(
                    f"Could not submit worker ({len(compensated_ids)} workers compensated: {compensated_ids}): {exc}"
                ) from exc

        if bool(args.get("wait", False)):
            default_wait = max(record.timeout_seconds for record in records)
            waves = math.ceil(len(records) / self.max_concurrent)
            wait_timeout = self._bounded_int(
                args.get("wait_timeout_seconds", default_wait * waves + 60),
                default=default_wait * waves + 60,
                minimum=1,
                maximum=7200,
            )
            return {
                "success": True,
                "action": "spawn",
                "waited": True,
                "results": self._wait_for(records, wait_timeout),
            }
        return {
            "success": True,
            "action": "spawn",
            "waited": False,
            "workers": [self._summary(record) for record in records],
            "note": "Workers are running in the background. Use action=wait or action=result.",
        }

    def _list(self, owner_id: str) -> dict[str, Any]:
        with self._lock:
            records = [record for record in self._records.values() if record.owner_id == owner_id]
            records.sort(key=lambda item: item.created_at, reverse=True)
        return {
            "success": True,
            "action": "list",
            "count": len(records),
            "workers": [self._summary(record) for record in records[:100]],
        }

    def _wait_action(self, args: dict[str, Any], owner_id: str) -> dict[str, Any]:
        requested_ids = args.get("worker_ids")
        if requested_ids is None and args.get("worker_id"):
            requested_ids = [args["worker_id"]]
        elif isinstance(requested_ids, str):
            requested_ids = [requested_ids]
        elif requested_ids is not None and not isinstance(requested_ids, (list, tuple)):
            raise ValueError("worker_ids must be a list of strings")
        if requested_ids is None:
            with self._lock:
                records = [
                    record
                    for record in self._records.values()
                    if record.owner_id == owner_id and record.status not in TERMINAL_STATUSES
                ]
        else:
            records = [self._owned_record(owner_id, str(worker_id)) for worker_id in requested_ids]
        timeout = self._bounded_int(
            args.get("wait_timeout_seconds", self.default_timeout + 60),
            default=self.default_timeout + 60,
            minimum=1,
            maximum=7200,
        )
        return {
            "success": True,
            "action": "wait",
            "results": self._wait_for(records, timeout),
        }

    def _stop(self, args: dict[str, Any], owner_id: str) -> dict[str, Any]:
        worker_id = self._validate_text(args.get("worker_id"), "worker_id", required=True)
        record = self._owned_record(owner_id, worker_id)
        with self._lock:
            if record.status in TERMINAL_STATUSES:
                return {"success": True, "action": "stop", "worker": self._summary(record)}
            record.cancel_requested = True
            future = self._futures.get(worker_id)
            ownership = self._processes.get(worker_id)
            if future is not None and future.cancel():
                self._transition(
                    record,
                    "cancelled",
                    error="Cancelled before the worker process started.",
                    finished=True,
                )
            else:
                self._write_record(record)
        if ownership is not None:
            if not self._drop_process_if_quiescent(
                worker_id,
                ownership,
                terminate=True,
            ):
                self._ensure_resource_reaper(worker_id)
        return {"success": True, "action": "stop", "worker": self._summary(record)}

    def _steer(self, args: dict[str, Any], owner_id: str) -> dict[str, Any]:
        worker_id = self._validate_text(args.get("worker_id"), "worker_id", required=True)
        message = self._validate_text(args.get("message"), "message", required=True, max_len=self.max_steer_message_chars)
        record = self._owned_record(owner_id, worker_id)
        with self._lock:
            if record.status in TERMINAL_STATUSES:
                raise ValueError("Worker already finished. Use action=continue instead.")
            if len(record.steer_queue) >= self.max_steer_queue_size:
                raise ValueError(f"Steer queue is full: maximum is {self.max_steer_queue_size} messages")
            record.steer_queue.append(message)
            self._write_record(record)
        return {
            "success": True,
            "action": "steer",
            "worker": self._summary(record),
            "queued_steers": len(record.steer_queue),
            "note": "The message will run as a continuation after the current agy print call.",
        }

    def _continue(self, args: dict[str, Any], owner_id: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        worker_id = self._validate_text(args.get("worker_id"), "worker_id", required=True)
        message = self._validate_text(args.get("message"), "message", required=True, max_len=64000)
        source = self._owned_record(owner_id, worker_id)
        if source.status not in TERMINAL_STATUSES:
            raise ValueError("Worker is still active. Use action=steer instead.")
        if not source.conversation_id:
            raise ValueError("Worker has no conversation_id and cannot be continued.")
        requested_task_type = args.get("task_type")
        continuation_model = args.get("model")
        if not continuation_model and not requested_task_type:
            continuation_model = source.model
        raw_spec = {
            "goal": message,
            "context": args.get("context") or "",
            "task_type": requested_task_type or source.task_type,
            "model": continuation_model or "",
            "execution_mode": args.get("execution_mode") or source.execution_mode,
            "effort": args.get("effort") if args.get("effort") is not None else source.effort,
            "workdir": args.get("workdir") or source.workdir,
            "timeout_seconds": args.get("timeout_seconds") or source.timeout_seconds,
        }
        spec = self._task_spec(raw_spec, kwargs.get("workdir") or kwargs.get("cwd"))

        with self._lock:
            if self._closed:
                raise RuntimeError("delegate-agy is shutting down")
            if self._executor is None:
                raise RuntimeError("delegate-agy executor is unavailable")
            active_global = sum(1 for r in self._records.values() if r.status not in TERMINAL_STATUSES)
            if active_global + 1 > self.max_pending_workers:
                raise ValueError(
                    f"Global worker capacity exceeded: maximum pending workers is {self.max_pending_workers}"
                )
            active_owner = sum(
                1 for r in self._records.values() if r.owner_id == owner_id and r.status not in TERMINAL_STATUSES
            )
            if active_owner + 1 > self.max_pending_per_owner:
                raise ValueError(
                    f"Session worker capacity exceeded: maximum pending workers per session is {self.max_pending_per_owner}"
                )

            new_worker_id = f"agy_{uuid.uuid4().hex[:12]}"
            record = RunRecord(
                worker_id=new_worker_id,
                owner_id=owner_id,
                conversation_id=source.conversation_id,
                parent_worker_id=source.worker_id,
                runtime_id=self.runtime_id,
                supervisor_pid=self.supervisor_pid,
                supervisor_started_at=self.supervisor_started_at,
                **spec,
            )
            self._write_record(record)
            self._records[new_worker_id] = record
            future = self._executor.submit(self._supervise_worker, new_worker_id)
            self._futures[new_worker_id] = future
            future.add_done_callback(
                lambda completed, target=new_worker_id: self._future_done(target, completed)
            )

        if bool(args.get("wait", False)):
            wait_timeout = self._bounded_int(
                args.get("wait_timeout_seconds", record.timeout_seconds + 60),
                default=record.timeout_seconds + 60,
                minimum=1,
                maximum=7200,
            )
            return {
                "success": True,
                "action": "continue",
                "waited": True,
                "result": self._wait_for([record], wait_timeout)[0],
            }
        return {
            "success": True,
            "action": "continue",
            "waited": False,
            "worker": self._summary(record),
        }

    def handle(self, args: dict[str, Any], **kwargs: Any) -> str:
        """Dispatch a model tool call and return a JSON string."""
        try:
            action = str(args.get("action") or "spawn")
            owner_id = self._owner_id(kwargs)
            if action == "spawn":
                payload = self._spawn(args, owner_id, kwargs)
            elif action == "list":
                payload = self._list(owner_id)
            elif action in {"status", "result"}:
                worker_id = self._validate_text(
                    args.get("worker_id"), "worker_id", required=True
                )
                record = self._owned_record(owner_id, worker_id)
                payload = {
                    "success": True,
                    "action": action,
                    "worker": self._result(record) if action == "result" else self._summary(record),
                }
            elif action == "wait":
                payload = self._wait_action(args, owner_id)
            elif action == "stop":
                payload = self._stop(args, owner_id)
            elif action == "steer":
                payload = self._steer(args, owner_id)
            elif action == "continue":
                payload = self._continue(args, owner_id, kwargs)
            else:
                raise ValueError(f"Unknown action: {action}")
            return _json_text(payload)
        except Exception as exc:
            return _json_text({"success": False, "error": str(exc)})

    def shutdown(self) -> None:
        """Cancel queued work and terminate active subprocesses on plugin unload."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = list(self._processes.items())
            futures = list(self._futures.values())
            for worker_id, future in list(self._futures.items()):
                record = self._records.get(worker_id)
                if record is None:
                    continue
                if record.status not in TERMINAL_STATUSES:
                    record.cancel_requested = True
                if future.cancel():
                    if record.status not in TERMINAL_STATUSES:
                        try:
                            self._transition(
                                record,
                                "cancelled",
                                error="Cancelled while delegate-agy was shutting down.",
                                finished=True,
                            )
                        except OSError:
                            record.status = "cancelled"
                            record.error = "Cancelled while delegate-agy was shutting down."
                            record.finished_at = record.finished_at or _now()
                elif record.status not in TERMINAL_STATUSES:
                    try:
                        self._write_record(record)
                    except OSError:
                        pass
            for record in list(self._records.values()):
                if record.status not in TERMINAL_STATUSES and record.worker_id not in self._futures:
                    try:
                        self._transition(
                            record,
                            "cancelled",
                            error="Cancelled while delegate-agy was shutting down.",
                            finished=True,
                        )
                    except OSError:
                        record.status = "cancelled"
                        record.error = "Cancelled while delegate-agy was shutting down."
                        record.finished_at = record.finished_at or _now()
        try:
            for worker_id, ownership in active:
                self._drop_process_if_quiescent(
                    worker_id,
                    ownership,
                    terminate=True,
                )
            deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    future.result(timeout=remaining)
                except (FutureTimeout, Exception):
                    pass
        finally:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                remaining_futures = [
                    future for future in self._futures.values() if not future.done()
                ]
                processes_remain = bool(self._processes)
            if not remaining_futures and not processes_remain:
                self._release_runtime_lease()
            else:
                self._ensure_resource_reaper()


def register_tools(ctx) -> None:
    """Register delegate_agy for CLI/TUI preloading."""
    if __package__:
        from .schemas import DELEGATE_AGY_SCHEMA
    else:
        from schemas import DELEGATE_AGY_SCHEMA

    settings = {
        "max_concurrent": ctx.get_config("max_concurrent", 3),
        "max_pending_workers": ctx.get_config("max_pending_workers", 20),
        "max_pending_per_owner": ctx.get_config("max_pending_per_owner", 10),
        "default_timeout_seconds": ctx.get_config("default_timeout_seconds", 600),
        "max_result_chars": ctx.get_config("max_result_chars", 24000),
        "max_records_in_memory": ctx.get_config("max_records_in_memory", 500),
        "max_raw_output_bytes": ctx.get_config("max_raw_output_bytes", 16 * 1024 * 1024),
        "retention_days": ctx.get_config("retention_days", 30),
        "max_total_storage_mb": ctx.get_config("max_total_storage_mb", 1024),
        "allow_accept_edits": ctx.get_config("allow_accept_edits", False),
    }
    manager = None
    try:
        manager = AgyManager(settings=settings)
        ctx.on_unload(manager.shutdown)
        registration = ctx.register_tool(
            name="delegate_agy",
            toolset="delegate_agy",
            schema=DELEGATE_AGY_SCHEMA,
            handler=manager.handle,
            check_fn=lambda: _check_agy_executable(manager.agy_path)[0],
            description="Delegate tasks to model-routed Antigravity CLI workers.",
            emoji="🛰️",
        )
        if registration is None:
            raise RuntimeError("Hermes rejected delegate_agy tool registration")
    except BaseException:
        if manager is not None:
            try:
                manager.shutdown()
            except BaseException:
                pass
        raise
