"""Process manager for model-routed Antigravity CLI workers."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
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
except ImportError:
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
PROCESS_GROUP_TERM_GRACE_SECONDS = 5.0
PROCESS_REAP_TIMEOUT_SECONDS = 1.0
PIPE_DRAIN_TIMEOUT_SECONDS = 1.0
SHUTDOWN_WAIT_SECONDS = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


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
            settings.get("allow_accept_edits", True), default=True
        )
        self.agy_path = agy_path or shutil.which("agy") or "agy"
        self.data_dir = plugin_data_dir("delegate-agy")
        self.runs_dir = self.data_dir / "runs"
        self.runtime_dir = self.data_dir / "runtime"
        for directory in (self.data_dir, self.runs_dir, self.runtime_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
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

    def _acquire_runtime_lease(self) -> None:
        if fcntl is None:
            raise RuntimeError("delegate-agy runtime leases require fcntl")
        path = self._runtime_lease_path(self.runtime_id)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = json.dumps(
                {
                    "runtime_id": self.runtime_id,
                    "supervisor_pid": self.supervisor_pid,
                    "supervisor_started_at": self.supervisor_started_at,
                }
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.write(fd, metadata)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        with self._lease_lock:
            self._runtime_lease_fd = fd

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

    def _runtime_lease_is_locked(self, runtime_id: str) -> bool:
        if fcntl is None:
            return False
        try:
            path = self._runtime_lease_path(runtime_id)
        except ValueError:
            return False
        try:
            fd = os.open(path, os.O_RDWR)
        except (FileNotFoundError, OSError):
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return True
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _runtime_lease_path(self, runtime_id: str) -> Path:
        if not isinstance(runtime_id, str) or not _RUNTIME_ID_RE.fullmatch(runtime_id):
            raise ValueError(f"Invalid runtime_id format: {runtime_id!r}")
        path = (self.runtime_dir / f"{runtime_id}.lock").resolve()
        if not path.is_relative_to(self.runtime_dir.resolve()):
            raise ValueError(f"runtime_id path traversal detected: {runtime_id!r}")
        return path

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
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

    def _load_existing_records(self) -> None:
        paths = sorted(self.runs_dir.glob("agy_*.json"), key=self._path_mtime)
        for path in paths:
            record = self._validated_record_from_path(path)
            if record is None:
                continue
            if (
                record.status not in TERMINAL_STATUSES
                and record.runtime_id
                and self._runtime_lease_is_locked(record.runtime_id)
            ):
                continue
            if record.status not in TERMINAL_STATUSES:
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
            if record.runtime_id and self._runtime_lease_is_locked(record.runtime_id):
                raise ValueError(
                    f"Worker is externally supervised by runtime {record.runtime_id}: {worker_id}"
                )
            record.status = "interrupted"
            record.error = "Hermes/plugin runtime ended before this worker completed."
            record.finished_at = _now()
            self._write_record(record)
        return record

    def _validated_record_from_path(self, path: Path) -> RunRecord | None:
        """Load one confined, internally consistent record without rewriting it."""
        try:
            runs_root = self.runs_dir.resolve()
            if path.is_symlink():
                return None
            resolved = path.resolve()
            if not resolved.is_relative_to(runs_root) or resolved.parent != runs_root:
                return None
            worker_id_from_filename = resolved.stem
            if not _WORKER_ID_RE.fullmatch(worker_id_from_filename):
                return None
            raw = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            known_fields = {item.name for item in fields(RunRecord)}
            record = RunRecord(
                **{key: value for key, value in raw.items() if key in known_fields}
            )
        except (OSError, ValueError, TypeError):
            return None

        if not isinstance(record.worker_id, str):
            return None
        if record.worker_id != worker_id_from_filename:
            return None
        if not _WORKER_ID_RE.fullmatch(record.worker_id):
            return None
        owner_id = str(record.owner_id).strip()
        if not owner_id:
            return None
        record.owner_id = owner_id
        if record.runtime_id is not None:
            if not isinstance(record.runtime_id, str):
                return None
            if not _RUNTIME_ID_RE.fullmatch(record.runtime_id):
                return None
        return record

    def _record_path(self, worker_id: str) -> Path:
        if not isinstance(worker_id, str) or not _WORKER_ID_RE.fullmatch(worker_id):
            raise ValueError(f"Invalid worker_id format: {worker_id!r}")
        path = (self.runs_dir / f"{worker_id}.json").resolve()
        if not path.is_relative_to(self.runs_dir.resolve()):
            raise ValueError(f"worker_id path traversal detected: {worker_id!r}")
        return path

    def _write_record(self, record: RunRecord) -> None:
        path = self._record_path(record.worker_id)
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    asdict(record),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
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
        owner_id = kwargs.get("session_id") or kwargs.get("task_id")
        if not owner_id:
            raise ValueError("Hermes did not provide task_id or session_id; refusing unscoped access")
        return str(owner_id)

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
        truncated = len(response) > self.max_result_chars
        if truncated:
            response = response[: self.max_result_chars] + "\n[truncated; inspect stdout_path]"
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
    def _validate_text(value: Any, field_name: str, *, required: bool = False) -> str:
        text = "" if value is None else str(value).strip()
        if required and not text:
            raise ValueError(f"{field_name} is required")
        if len(text) > 100000:
            raise ValueError(f"{field_name} exceeds 100000 characters")
        return text

    def _task_spec(self, raw: dict[str, Any], default_workdir: str | None) -> dict[str, Any]:
        goal = self._validate_text(raw.get("goal"), "goal", required=True)
        context = self._validate_text(raw.get("context"), "context")
        task_type = str(raw.get("task_type") or "investigation")
        if task_type not in MODEL_BY_TASK_TYPE:
            raise ValueError(f"Unknown task_type: {task_type}")
        explicit_model = self._validate_text(raw.get("model"), "model")
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

    def _new_record(
        self,
        owner_id: str,
        spec: dict[str, Any],
        *,
        conversation_id: str | None = None,
        parent_worker_id: str | None = None,
    ) -> RunRecord:
        worker_id = f"agy_{uuid.uuid4().hex[:12]}"
        record = RunRecord(
            worker_id=worker_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            parent_worker_id=parent_worker_id,
            runtime_id=self.runtime_id,
            supervisor_pid=self.supervisor_pid,
            supervisor_started_at=self.supervisor_started_at,
            **spec,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("delegate-agy is shutting down")
            try:
                self._write_record(record)
            except Exception as exc:
                try:
                    self._record_path(record.worker_id).unlink(missing_ok=True)
                except OSError:
                    pass
                raise RuntimeError(f"Initial record persistence failed: {exc}") from exc
            self._records[worker_id] = record
            try:
                if self._executor is None:
                    raise RuntimeError("delegate-agy executor is unavailable")
                future = self._executor.submit(self._supervise_worker, worker_id)
            except Exception as exc:
                record.status = "failed"
                record.finished_at = _now()
                record.error = f"Could not submit worker: {exc}"
                try:
                    self._write_record(record)
                except OSError:
                    pass
                raise RuntimeError(record.error) from exc
            self._futures[worker_id] = future
            future.add_done_callback(
                lambda completed, target=worker_id: self._future_done(target, completed)
            )
        return record

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
            command.append("--dangerously-skip-permissions")
        else:
            command.extend(["--sandbox", "--dangerously-skip-permissions"])
        return command

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any] | None:
        cleaned = stdout.strip()
        if not cleaned:
            return None

        def is_result_payload(candidate: Any) -> bool:
            if not isinstance(candidate, dict):
                return False
            has_response = candidate.get("response") is not None
            has_conv_id = bool(candidate.get("conversation_id"))
            has_error = candidate.get("error") is not None
            status = candidate.get("status")
            status_str = str(status).upper() if isinstance(status, str) else None

            if status_str in {"SUCCESS", "SUCCEEDED", "DONE", "COMPLETED"}:
                return has_response or has_conv_id
            if status_str in {"ERROR", "FAILED"}:
                return has_error
            if (has_response or has_conv_id) and not any(k in candidate for k in ("event", "level", "logger")):
                return True
            if has_error and not any(k in candidate for k in ("event", "level", "logger")):
                return True
            return False

        try:
            payload = json.loads(cleaned)
            return payload if is_result_payload(payload) else None
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
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
            except json.JSONDecodeError:
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
            if conversation_id:
                record.conversation_id = str(conversation_id)
            if "response" in payload and payload.get("response") is not None:
                record.response = str(payload["response"])
            usage = payload.get("usage")
            record.usage = usage if isinstance(usage, dict) else {}
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

        structured_error = payload.get("error") if payload is not None else None
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
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _bounded_drain(
        self, process: subprocess.Popen[str]
    ) -> tuple[str, str, str | None]:
        try:
            stdout, stderr = process.communicate(timeout=PIPE_DRAIN_TIMEOUT_SECONDS)
            return stdout or "", stderr or "", None
        except subprocess.TimeoutExpired as exc:
            stdout = exc.output if isinstance(exc.output, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            self._close_process_pipes(process)
            return stdout, stderr, "Timed out while draining terminated agy output pipes."
        except (OSError, ValueError) as exc:
            self._close_process_pipes(process)
            return "", "", f"Could not drain terminated agy output pipes: {exc}"

    def _terminate_process(
        self, process: subprocess.Popen[str], pgid: int | None
    ) -> bool:
        """Boundedly terminate owned work and report whether ownership is quiescent."""
        if os.name == "posix" and self._captured_group_is_owned(process, pgid):
            assert pgid is not None
            if self._group_exists(pgid):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                deadline = time.monotonic() + PROCESS_GROUP_TERM_GRACE_SECONDS
                while time.monotonic() < deadline and self._group_exists(pgid):
                    time.sleep(0.02)
                if self._group_exists(pgid):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
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
            self._reap_worker_ids.update(worker_ids)
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
                    resources_remain = bool(self._processes) or any(
                        not future.done() for future in self._futures.values()
                    )
                if closed and resources_remain:
                    self._ensure_resource_reaper()
                elif closed:
                    self._release_runtime_lease()

    def _execute_once(self, record: RunRecord, prompt: str) -> bool:
        command = self._build_command(record, prompt)
        creation: dict[str, Any] = {}
        if os.name == "posix":
            creation["start_new_session"] = True
        process: subprocess.Popen[str]
        pgid: int | None = None
        try:
            process = subprocess.Popen(
                command,
                cwd=record.workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
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
            try:
                if os.name == "posix":
                    pgid = os.getpgid(process.pid)
                    if not self._captured_group_is_owned(process, pgid):
                        raise RuntimeError(
                            f"Refusing unsafe process group pid={process.pid} pgid={pgid}"
                        )
                with self._lock:
                    self._processes[record.worker_id] = OwnedProcess(process, pgid)
                    record.pid = process.pid
                    record.pgid = pgid
                    try:
                        self._write_record(record)
                    except OSError as exc:
                        raise RuntimeError(f"Post-spawn persistence failed: {exc}") from exc
                    cancel_requested = record.cancel_requested
            except Exception as exc:
                self._terminate_process(process, pgid)
                self._bounded_drain(process)
                with self._lock:
                    record.returncode = process.returncode
                    try:
                        self._transition(record, "failed", error=str(exc), finished=True)
                    except OSError:
                        record.status = "failed"
                        record.error = str(exc)
                        record.finished_at = record.finished_at or _now()
                return False

            if cancel_requested:
                self._terminate_process(process, pgid)

            try:
                stdout, stderr = process.communicate(timeout=record.timeout_seconds + 30)
            except subprocess.TimeoutExpired:
                self._terminate_process(process, pgid)
                stdout, stderr, drain_error = self._bounded_drain(process)
                stdout_path, stderr_path = self._save_raw_output(record, stdout, stderr)
                error = f"agy exceeded the outer timeout of {record.timeout_seconds + 30}s"
                if drain_error:
                    error = f"{error}; {drain_error}"
                with self._lock:
                    record.returncode = process.returncode
                    record.stdout_path = stdout_path
                    record.stderr_path = stderr_path
                    try:
                        self._transition(record, "timed_out", error=error, finished=True)
                    except OSError:
                        record.status = "timed_out"
                        record.error = error
                        record.finished_at = record.finished_at or _now()
                return False
            except Exception as exc:
                self._terminate_process(process, pgid)
                stdout, stderr, drain_error = self._bounded_drain(process)
                stdout_path, stderr_path = self._save_raw_output(record, stdout, stderr)
                error = f"Process communication failed: {exc}"
                if drain_error:
                    error = f"{error}; {drain_error}"
                with self._lock:
                    record.returncode = process.returncode
                    record.stdout_path = stdout_path
                    record.stderr_path = stderr_path
                    try:
                        self._transition(record, "failed", error=error, finished=True)
                    except OSError:
                        record.status = "failed"
                        record.error = error
                        record.finished_at = record.finished_at or _now()
                return False

            if os.name == "posix" and pgid is not None and self._group_exists(pgid):
                self._terminate_process(process, pgid)

            stdout_path, stderr_path = self._save_raw_output(record, stdout, stderr)
            payload = self._parse_payload(stdout)
            with self._lock:
                record.returncode = process.returncode
                record.stdout_path = stdout_path
                record.stderr_path = stderr_path
                successful = self._interpret_payload(
                    record,
                    payload,
                    returncode=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )
                self._write_record(record)
            return successful
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

    def _atomic_output_write(self, path: Path, content: str) -> str | None:
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            encoded = content.encode("utf-8")[: self.max_raw_output_bytes]
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            return str(path)
        except OSError:
            return None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _save_raw_output(
        self, record: RunRecord, stdout: str, stderr: str
    ) -> tuple[str | None, str | None]:
        attempt = max(1, record.attempt_index)
        stdout_path = self.runs_dir / f"{record.worker_id}.turn-{attempt:03d}.stdout"
        stderr_path = self.runs_dir / f"{record.worker_id}.turn-{attempt:03d}.stderr"
        return (
            self._atomic_output_write(stdout_path, stdout),
            self._atomic_output_write(stderr_path, stderr),
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
        cutoff = time.time() - self.retention_days * 86400
        for modified, worker_id, _paths in self._terminal_bundles():
            if modified < cutoff:
                self._delete_terminal_bundle(worker_id)

        limit_bytes = self.max_total_storage_mb * 1024 * 1024
        files = [
            path
            for path in self.runs_dir.iterdir()
            if path.is_file() and not path.is_symlink()
        ]
        total = sum(path.stat().st_size for path in files)
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
        records = [self._new_record(owner_id, spec) for spec in specs]
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
        message = self._validate_text(args.get("message"), "message", required=True)
        record = self._owned_record(owner_id, worker_id)
        with self._lock:
            if record.status in TERMINAL_STATUSES:
                raise ValueError("Worker already finished. Use action=continue instead.")
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
        message = self._validate_text(args.get("message"), "message", required=True)
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
        record = self._new_record(
            owner_id,
            spec,
            conversation_id=source.conversation_id,
            parent_worker_id=source.worker_id,
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
        "default_timeout_seconds": ctx.get_config("default_timeout_seconds", 600),
        "max_result_chars": ctx.get_config("max_result_chars", 24000),
        "max_records_in_memory": ctx.get_config("max_records_in_memory", 500),
        "max_raw_output_bytes": ctx.get_config("max_raw_output_bytes", 16 * 1024 * 1024),
        "retention_days": ctx.get_config("retention_days", 30),
        "max_total_storage_mb": ctx.get_config("max_total_storage_mb", 1024),
        "allow_accept_edits": ctx.get_config("allow_accept_edits", True),
    }
    manager = AgyManager(settings=settings)
    try:
        ctx.on_unload(manager.shutdown)
        registration = ctx.register_tool(
            name="delegate_agy",
            toolset="delegate_agy",
            schema=DELEGATE_AGY_SCHEMA,
            handler=manager.handle,
            check_fn=lambda: shutil.which("agy") is not None,
            description="Delegate tasks to model-routed Antigravity CLI workers.",
            emoji="🛰️",
        )
        if registration is None:
            raise RuntimeError("Hermes rejected delegate_agy tool registration")
    except Exception:
        manager.shutdown()
        raise
