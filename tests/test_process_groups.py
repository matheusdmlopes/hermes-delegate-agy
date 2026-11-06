"""Real local process-group lifecycle tests; no Antigravity quota is used."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "delegate_agy_process_group_test"


def load_manager_class():
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not create plugin import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{PACKAGE_NAME}.tools").AgyManager


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def read_pid_file(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return int(content)
            except (OSError, ValueError):
                pass
        time.sleep(0.02)
    raise AssertionError(f"Could not read valid PID from {path}")


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            return stat_path.read_text(encoding="utf-8").split()[2] != "Z"
        except (OSError, IndexError):
            pass
    return True


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProcessGroupTests(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("POSIX process groups are required")
        self.temp_home = tempfile.TemporaryDirectory(prefix="delegate-agy-pg-home-")
        self.temp_work = tempfile.TemporaryDirectory(prefix="delegate-agy-pg-work-")
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.temp_home.name})
        self.env_patch.start()
        self.AgyManager = load_manager_class()
        self.managers = []
        self.test_groups: set[int] = set()

    def tearDown(self):
        for pgid in self.test_groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for manager in self.managers:
            manager.shutdown()
        self.env_patch.stop()
        self.temp_work.cleanup()
        self.temp_home.cleanup()

    def _script(self, name: str, body: str) -> Path:
        script = Path(self.temp_work.name) / name
        script.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        script.chmod(0o755)
        return script

    def _manager(self, script: Path, *, max_concurrent: int = 1):
        manager = self.AgyManager(
            settings={"max_concurrent": max_concurrent, "default_timeout_seconds": 180},
            agy_path=str(script),
        )
        self.managers.append(manager)
        tools_module = sys.modules[self.AgyManager.__module__]
        setattr(tools_module, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.2)
        setattr(tools_module, "PROCESS_REAP_TIMEOUT_SECONDS", 0.5)
        setattr(tools_module, "PIPE_DRAIN_TIMEOUT_SECONDS", 0.5)
        return manager

    def _spawn(self, manager, **overrides):
        payload = {
            "action": "spawn",
            "goal": "Exercise a local fake process",
            "task_type": "simple",
            "workdir": self.temp_work.name,
            "timeout_seconds": 180,
        }
        payload.update(overrides)
        result = json.loads(manager.handle(payload, session_id="process-test"))
        self.assertTrue(result["success"], result)
        return result["workers"][0]["worker_id"]

    def _record_with_process(self, manager, worker_id: str):
        def ready():
            with manager._lock:
                return manager._records[worker_id].pid is not None

        self.assertTrue(wait_until(ready), "worker process did not start")
        with manager._lock:
            record = manager._records[worker_id]
            self.test_groups.add(record.pgid)
            return record.pid, record.pgid

    def _status(self, manager, worker_id: str) -> str:
        result = json.loads(
            manager.handle(
                {"action": "status", "worker_id": worker_id},
                session_id="process-test",
            )
        )
        self.assertTrue(result["success"], result)
        return result["worker"]["status"]

    def _assert_terminal(self, manager, worker_id: str, expected: set[str]):
        self.assertTrue(
            wait_until(lambda: self._status(manager, worker_id) in expected, timeout=5),
            f"worker did not reach {expected}: {self._status(manager, worker_id)}",
        )
        self.assertIn(self._status(manager, worker_id), expected)

    def _stop(self, manager, worker_id: str):
        result = json.loads(
            manager.handle(
                {"action": "stop", "worker_id": worker_id},
                session_id="process-test",
            )
        )
        self.assertTrue(result["success"], result)

    def test_stop_uses_validated_process_group_captured_at_spawn(self):
        script = self._script("normal-term", "import time\nwhile True: time.sleep(1)\n")
        manager = self._manager(script)
        worker_id = self._spawn(manager)
        pid, pgid = self._record_with_process(manager, worker_id)
        self.assertEqual(pgid, pid)
        self.assertNotEqual(pgid, os.getpgrp())

        tools_module = sys.modules[self.AgyManager.__module__]
        with patch.object(
            tools_module.os,
            "getpgid",
            side_effect=AssertionError("PGID was rediscovered during termination"),
        ):
            self._stop(manager, worker_id)

        self._assert_terminal(manager, worker_id, {"cancelled"})
        self.assertTrue(wait_until(lambda: not pid_exists(pid)))
        self.assertTrue(wait_until(lambda: not group_exists(pgid)))

    def test_sigkill_removes_process_that_ignores_sigterm(self):
        script = self._script(
            "ignore-term",
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: time.sleep(1)\n",
        )
        manager = self._manager(script)
        worker_id = self._spawn(manager)
        pid, pgid = self._record_with_process(manager, worker_id)

        self._stop(manager, worker_id)

        self._assert_terminal(manager, worker_id, {"cancelled"})
        self.assertTrue(wait_until(lambda: not pid_exists(pid)))
        self.assertTrue(wait_until(lambda: not group_exists(pgid)))

    def test_leader_exit_does_not_hide_live_descendant_group(self):
        child_file = Path(self.temp_work.name) / "orphan-child.pid"
        script = self._script(
            "leader-exits",
            "import os, signal, time\n"
            f"child_file = {str(child_file)!r}\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    open(child_file, 'w').write(str(os.getpid()))\n"
            "    while True: time.sleep(1)\n"
            "os._exit(0)\n",
        )
        manager = self._manager(script)
        worker_id = self._spawn(manager)
        _, pgid = self._record_with_process(manager, worker_id)
        child_pid = read_pid_file(child_file)

        self._stop(manager, worker_id)

        self._assert_terminal(manager, worker_id, {"cancelled"})
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))
        self.assertTrue(wait_until(lambda: not group_exists(pgid)))

    def test_descendant_holding_inherited_pipes_is_terminated(self):
        child_file = Path(self.temp_work.name) / "pipe-child.pid"
        script = self._script(
            "inherited-pipes",
            "import os, time\n"
            f"child_file = {str(child_file)!r}\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    open(child_file, 'w').write(str(os.getpid()))\n"
            "while True: time.sleep(1)\n",
        )
        manager = self._manager(script)
        worker_id = self._spawn(manager)
        pid, pgid = self._record_with_process(manager, worker_id)
        child_pid = read_pid_file(child_file)

        self._stop(manager, worker_id)

        self._assert_terminal(manager, worker_id, {"cancelled"})
        self.assertTrue(wait_until(lambda: not pid_exists(pid)))
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))
        self.assertTrue(wait_until(lambda: not group_exists(pgid)))

    def test_outer_timeout_drain_is_bounded_with_inherited_pipe(self):
        child_file = Path(self.temp_work.name) / "timeout-child.pid"
        script = self._script(
            "timeout-pipes",
            "import os, signal, time\n"
            f"child_file = {str(child_file)!r}\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    open(child_file, 'w').write(str(os.getpid()))\n"
            "    while True: time.sleep(1)\n"
            "while True: time.sleep(1)\n",
        )
        manager = self._manager(script)
        tools_module = sys.modules[self.AgyManager.__module__]
        record = tools_module.RunRecord(
            worker_id="agy_outer_timeout",
            owner_id="process-test",
            goal="timeout",
            context="",
            prompt="timeout",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=-29.8,
            status="running",
        )
        manager._records[record.worker_id] = record
        outcome = []
        thread = threading.Thread(
            target=lambda: outcome.append(manager._execute_once(record, record.prompt)),
            daemon=True,
        )
        thread.start()
        self.assertTrue(wait_until(lambda: record.pgid is not None))
        self.test_groups.add(record.pgid)
        child_pid = read_pid_file(child_file)

        thread.join(timeout=5)

        self.assertFalse(thread.is_alive(), "outer timeout left streaming drain unbounded")
        self.assertEqual(outcome, [False])
        self.assertEqual(record.status, "timed_out")
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))
        self.assertTrue(wait_until(lambda: not group_exists(record.pgid)))

    def test_first_post_spawn_persistence_failure_leaks_no_child(self):
        script = self._script("persist-failure", "import time\nwhile True: time.sleep(1)\n")
        manager = self._manager(script)
        original_write = manager._write_record
        failed = False

        def fail_first_post_spawn(record):
            nonlocal failed
            if record.pid is not None and not failed:
                failed = True
                raise OSError("injected post-spawn persistence failure")
            return original_write(record)

        with patch.object(manager, "_write_record", side_effect=fail_first_post_spawn):
            result = json.loads(
                manager.handle(
                    {
                        "action": "spawn",
                        "goal": "Persistence must fail safely",
                        "workdir": self.temp_work.name,
                        "wait": True,
                        "wait_timeout_seconds": 5,
                    },
                    session_id="process-test",
                )
            )

        self.assertTrue(result["success"], result)
        record = next(iter(manager._records.values()))
        self.test_groups.add(record.pgid)
        self.assertEqual(record.status, "failed")
        self.assertIn("persistence", record.error)
        self.assertTrue(wait_until(lambda: not pid_exists(record.pid)))
        self.assertTrue(wait_until(lambda: not group_exists(record.pgid)))
        self.assertEqual(manager._processes, {})

    def test_natural_success_cleans_up_descendant_process_group(self):
        child_file = Path(self.temp_work.name) / "natural-child.pid"
        script = self._script(
            "natural-success-fork",
            "import os, signal, sys, time, json\n"
            f"child_file = {str(child_file)!r}\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    try:\n"
            "        os.close(0)\n"
            "    except OSError: pass\n"
            "    try:\n"
            "        os.close(1)\n"
            "    except OSError: pass\n"
            "    try:\n"
            "        os.close(2)\n"
            "    except OSError: pass\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    with open(child_file, 'w') as f: f.write(str(os.getpid()))\n"
            "    while True: time.sleep(1)\n"
            "print(json.dumps({'status': 'SUCCESS', 'response': 'Leader done', 'conversation_id': 'c1'}))\n"
            "sys.stdout.flush()\n"
            "sys.exit(0)\n",
        )
        manager = self._manager(script)
        worker_id = self._spawn(manager)
        _, pgid = self._record_with_process(manager, worker_id)
        child_pid = read_pid_file(child_file)

        self._assert_terminal(manager, worker_id, {"succeeded"})
        self.assertTrue(wait_until(lambda: not pid_exists(child_pid)))
        self.assertTrue(wait_until(lambda: not group_exists(pgid)))
        self.assertEqual(manager._processes, {})

    def test_stream_drain_oserror_cleans_up_process_group(self):
        script = self._script("comm-error", "import time\nwhile True: time.sleep(1)\n")
        manager = self._manager(script)
        tools_module = sys.modules[self.AgyManager.__module__]
        record = tools_module.RunRecord(
            worker_id="agy_comm_error",
            owner_id="process-test",
            goal="test comm error",
            context="",
            prompt="test comm error",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
            status="running",
        )
        manager._records[record.worker_id] = record

        with patch.object(
            manager,
            "_drain_and_stream",
            side_effect=OSError("injected stream drain I/O error"),
        ):
            success = manager._execute_once(record, record.prompt)

        self.assertFalse(success)
        self.assertEqual(record.status, "failed")
        self.assertIn("stream drain", record.error)
        if record.pgid:
            self.test_groups.add(record.pgid)
            self.assertTrue(wait_until(lambda: not pid_exists(record.pid)))
            self.assertTrue(wait_until(lambda: not group_exists(record.pgid)))
        self.assertEqual(manager._processes, {})

    def test_process_ownership_is_retained_until_group_death_is_confirmed(self):
        script = self._script("unused-cleanup-probe", "raise SystemExit(99)\n")
        manager = self._manager(script)
        tools_module = sys.modules[self.AgyManager.__module__]
        record = tools_module.RunRecord(
            worker_id="agy_cleanup_order",
            owner_id="process-test",
            goal="cleanup ordering",
            context="",
            prompt="cleanup ordering",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
            status="running",
        )
        manager._records[record.worker_id] = record
        process = MagicMock()
        process.pid = 424242
        process.returncode = 0
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.communicate.return_value = (
            json.dumps({"status": "SUCCESS", "response": "done"}),
            "",
        )
        group_alive = True

        def reports_group_liveness(_pgid):
            return group_alive

        with (
            patch.object(tools_module.subprocess, "Popen", return_value=process),
            patch.object(tools_module.os, "getpgid", return_value=process.pid),
            patch.object(tools_module.os, "getpgrp", return_value=999999),
            patch.object(tools_module.os, "killpg", return_value=None),
            patch.object(manager, "_group_exists", side_effect=reports_group_liveness),
            patch.object(tools_module, "PROCESS_GROUP_TERM_GRACE_SECONDS", 0.01),
            patch.object(tools_module, "PROCESS_REAP_TIMEOUT_SECONDS", 0.01),
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup was not confirmed"):
                manager._execute_once(record, record.prompt)

            self.assertIn(record.worker_id, manager._processes)
            self.assertIsNotNone(manager._runtime_lease_fd)
            manager.shutdown()
            self.assertIsNotNone(
                manager._runtime_lease_fd,
                "shutdown released lease while owned process group remained live",
            )
            group_alive = False
            self.assertTrue(
                wait_until(lambda: record.worker_id not in manager._processes),
                "resource reaper did not release confirmed-dead process ownership",
            )
            self.assertTrue(
                wait_until(lambda: manager._runtime_lease_fd is None),
                "resource reaper did not release lease after process ownership ended",
            )

if __name__ == "__main__":
    unittest.main()
