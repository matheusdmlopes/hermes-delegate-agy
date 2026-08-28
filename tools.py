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
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        self.agy_path = agy_path or shutil.which("agy") or "agy"
        self.data_dir = plugin_data_dir("delegate-agy")
        self.runs_dir = self.data_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._records: dict[str, RunRecord] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._futures: dict[str, Future[None]] = {}
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_concurrent,
            thread_name_prefix="delegate-agy",
        )
        self._load_existing_records()

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    def _load_existing_records(self) -> None:
        paths = sorted(
            self.runs_dir.glob("agy_*.json"),
            key=lambda path: path.stat().st_mtime,
        )[-200:]
        known_fields = {item.name for item in fields(RunRecord)}
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                record = RunRecord(
                    **{key: value for key, value in raw.items() if key in known_fields}
                )
            except (OSError, ValueError, TypeError):
                continue
            if record.status not in TERMINAL_STATUSES:
                record.status = "interrupted"
                record.error = "Hermes/plugin runtime ended before this worker completed."
                record.finished_at = _now()
                self._write_record(record)
            self._records[record.worker_id] = record

    def _record_path(self, worker_id: str) -> Path:
        return self.runs_dir / f"{worker_id}.json"

    def _write_record(self, record: RunRecord) -> None:
        path = self._record_path(record.worker_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _persist(self, record: RunRecord) -> None:
        with self._lock:
            self._records[record.worker_id] = record
            self._write_record(record)

    @staticmethod
    def _owner_id(kwargs: dict[str, Any]) -> str:
        owner_id = kwargs.get("task_id") or kwargs.get("session_id")
        if not owner_id:
            raise ValueError("Hermes did not provide task_id or session_id; refusing unscoped access")
        return str(owner_id)

    def _owned_record(self, owner_id: str, worker_id: str) -> RunRecord:
        with self._lock:
            record = self._records.get(worker_id)
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
            **spec,
        )
        self._persist(record)
        with self._lock:
            future = self._executor.submit(self._run_worker, worker_id)
            self._futures[worker_id] = future
        return record

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
            command.extend(["--mode", "accept-edits", "--dangerously-skip-permissions"])
        else:
            command.extend(["--sandbox", "--dangerously-skip-permissions"])
        return command

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any] | None:
        cleaned = stdout.strip()
        if not cleaned:
            return None
        try:
            payload = json.loads(cleaned)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass
        for line in reversed(cleaned.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _terminate_process(
        self, process: subprocess.Popen[str], pgid: int | None
    ) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix" and pgid is not None and pgid > 0:
                os.killpg(pgid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix" and pgid is not None and pgid > 0:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass

    def _execute_once(self, record: RunRecord, prompt: str) -> bool:
        command = self._build_command(record, prompt)
        creation: dict[str, Any] = {}
        if os.name == "posix":
            creation["start_new_session"] = True
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
                self._write_record(record)
            return False

        with self._lock:
            self._processes[record.worker_id] = process
            record.pid = process.pid
            record.pgid = process.pid if os.name == "posix" else None
            self._write_record(record)
            if record.cancel_requested:
                self._terminate_process(process, record.pgid)

        try:
            stdout, stderr = process.communicate(timeout=record.timeout_seconds + 30)
        except subprocess.TimeoutExpired:
            self._terminate_process(process, record.pgid)
            stdout, stderr = process.communicate()
            stdout_path, stderr_path = self._save_raw_output(record.worker_id, stdout, stderr)
            with self._lock:
                record.status = "timed_out"
                record.error = f"agy exceeded the outer timeout of {record.timeout_seconds + 30}s"
                record.returncode = process.returncode
                record.stdout_path = stdout_path
                record.stderr_path = stderr_path
                self._write_record(record)
            return False
        finally:
            with self._lock:
                self._processes.pop(record.worker_id, None)

        stdout_path, stderr_path = self._save_raw_output(record.worker_id, stdout, stderr)
        payload = self._parse_payload(stdout)
        with self._lock:
            record.returncode = process.returncode
            record.stdout_path = stdout_path
            record.stderr_path = stderr_path
            if payload is not None:
                conversation_id = payload.get("conversation_id")
                if conversation_id:
                    record.conversation_id = str(conversation_id)
                record.response = str(payload.get("response") or "")
                usage = payload.get("usage")
                record.usage = usage if isinstance(usage, dict) else {}
                duration = payload.get("duration_seconds")
                record.duration_seconds = float(duration) if isinstance(duration, (int, float)) else None
                turns = payload.get("num_turns")
                record.num_turns = int(turns) if isinstance(turns, (int, float)) else None
                agy_status = str(payload.get("status") or "").upper()
            else:
                agy_status = ""

            successful = process.returncode == 0 and payload is not None and agy_status in {
                "SUCCESS",
                "SUCCEEDED",
                "DONE",
                "COMPLETED",
            }
            if not successful:
                details = stderr.strip() or stdout.strip() or "agy returned no structured output"
                record.error = (
                    f"agy failed with return code {process.returncode}"
                    f" and status {agy_status or 'unknown'}: {details[:2000]}"
                )
            else:
                record.error = None
            self._write_record(record)
        return successful

    @staticmethod
    def _atomic_output_write(path: Path, content: str) -> str | None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
            return str(path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _save_raw_output(
        self, worker_id: str, stdout: str, stderr: str
    ) -> tuple[str | None, str | None]:
        stdout_path = self.runs_dir / f"{worker_id}.stdout"
        stderr_path = self.runs_dir / f"{worker_id}.stderr"
        return (
            self._atomic_output_write(stdout_path, stdout),
            self._atomic_output_write(stderr_path, stderr),
        )

    def _run_worker(self, worker_id: str) -> None:
        with self._lock:
            record = self._records[worker_id]
            if record.cancel_requested:
                record.status = "cancelled"
                record.finished_at = _now()
                self._write_record(record)
                return
            record.status = "running"
            record.started_at = _now()
            prompt = record.prompt
            self._write_record(record)

        while True:
            successful = self._execute_once(record, prompt)
            with self._lock:
                if record.cancel_requested:
                    record.status = "cancelled"
                    record.error = record.error or "Cancelled by the parent Hermes session."
                    record.finished_at = _now()
                    self._write_record(record)
                    return
                if not successful:
                    if record.status != "timed_out":
                        record.status = "failed"
                    record.finished_at = _now()
                    self._write_record(record)
                    return
                if record.steer_queue:
                    if not record.conversation_id:
                        record.status = "failed"
                        record.error = "agy returned no conversation_id, so queued steering cannot continue."
                        record.finished_at = _now()
                        self._write_record(record)
                        return
                    prompt = record.steer_queue.pop(0)
                    record.status = "continuing"
                    self._write_record(record)
                    continue
                record.status = "succeeded"
                record.finished_at = _now()
                self._write_record(record)
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
                    record.status = "failed"
                    record.error = f"Worker supervisor failed: {exc}"
                    record.finished_at = _now()
                    self._write_record(record)
        with self._lock:
            return [self._result(self._records[record.worker_id]) for record in records]

    def _spawn(self, args: dict[str, Any], owner_id: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("delegate-agy is shutting down")
        tasks = args.get("tasks")
        if tasks is not None:
            if not isinstance(tasks, list) or not tasks:
                raise ValueError("tasks must be a non-empty array")
            if len(tasks) > 10:
                raise ValueError("At most 10 tasks may be spawned in one call")
            raw_specs = tasks
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
            process = self._processes.get(worker_id)
            if future is not None and future.cancel():
                record.status = "cancelled"
                record.finished_at = _now()
            self._write_record(record)
        if process is not None:
            self._terminate_process(process, record.pgid)
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
            active = [
                (process, self._records[worker_id].pgid)
                for worker_id, process in self._processes.items()
            ]
            for record in self._records.values():
                if record.status not in TERMINAL_STATUSES:
                    record.cancel_requested = True
                    self._write_record(record)
        for process, pgid in active:
            self._terminate_process(process, pgid)
        self._executor.shutdown(wait=False, cancel_futures=True)

def register_tools(ctx) -> None:
    """Register delegate_agy for CLI/TUI preloading."""
    try:
        from .schemas import DELEGATE_AGY_SCHEMA
    except (ImportError, ValueError, KeyError):
        from schemas import DELEGATE_AGY_SCHEMA

    settings = {
        "max_concurrent": ctx.get_config("max_concurrent", 3),
        "default_timeout_seconds": ctx.get_config("default_timeout_seconds", 600),
        "max_result_chars": ctx.get_config("max_result_chars", 24000),
    }
    manager = AgyManager(settings=settings)
    ctx.on_unload(manager.shutdown)
    ctx.register_tool(
        name="delegate_agy",
        toolset="delegate_agy",
        schema=DELEGATE_AGY_SCHEMA,
        handler=manager.handle,
        check_fn=lambda: shutil.which("agy") is not None,
        description="Delegate tasks to model-routed Antigravity CLI workers.",
        emoji="🛰️",
    )
