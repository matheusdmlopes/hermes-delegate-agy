"""Behavioral tests for stop/spawn/shutdown races using local fake workers."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools as tools_module
from tools import AgyManager, TERMINAL_STATUSES


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _wait_for_lines(path: Path, count: int, timeout: float) -> bool:
    def enough_lines() -> bool:
        try:
            return len(path.read_text(encoding="utf-8").splitlines()) >= count
        except FileNotFoundError:
            return False

    return wait_until(enough_lines, timeout=timeout)


def pid_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ShutdownLifecycleTests(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("POSIX process groups are required")
        self.temp_home = tempfile.TemporaryDirectory(prefix="agy-shutdown-home-")
        self.temp_work = tempfile.TemporaryDirectory(prefix="agy-shutdown-work-")
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.temp_home.name})
        self.env_patch.start()
        self.test_groups: set[int] = set()
        self.managers: list[AgyManager] = []
        tools_module.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.2
        tools_module.PROCESS_REAP_TIMEOUT_SECONDS = 0.5
        tools_module.PIPE_DRAIN_TIMEOUT_SECONDS = 0.5
        tools_module.SHUTDOWN_WAIT_SECONDS = 2.0

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
        path = Path(self.temp_work.name) / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _manager(self, script: Path, max_concurrent: int = 1) -> AgyManager:
        manager = AgyManager(
            settings={"max_concurrent": max_concurrent, "default_timeout_seconds": 180},
            agy_path=str(script),
        )
        self.managers.append(manager)
        return manager

    def _spawn_batch(self, manager: AgyManager, count: int):
        result = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "tasks": [
                        {"goal": f"worker {index}", "workdir": self.temp_work.name}
                        for index in range(count)
                    ],
                },
                session_id="shutdown-session",
            )
        )
        self.assertTrue(result["success"], result)
        return [item["worker_id"] for item in result["workers"]]

    def _stop(self, manager: AgyManager, worker_id: str):
        return json.loads(
            manager.handle(
                {"action": "stop", "worker_id": worker_id},
                session_id="shutdown-session",
            )
        )

    def test_stop_before_future_starts_marks_queued_worker_cancelled(self):
        script = self._script("queue-blocker", "import time\nwhile True: time.sleep(1)\n")
        manager = self._manager(script)
        active_id, queued_id = self._spawn_batch(manager, 2)
        self.assertTrue(
            wait_until(lambda: manager._records[active_id].pid is not None),
            "first worker did not become active",
        )
        self.test_groups.add(manager._records[active_id].pgid)

        stopped = self._stop(manager, queued_id)

        self.assertTrue(stopped["success"], stopped)
        self.assertEqual(manager._records[queued_id].status, "cancelled")
        self.assertIsNotNone(manager._records[queued_id].finished_at)
        self._stop(manager, active_id)
        self.assertTrue(
            wait_until(lambda: manager._records[active_id].status == "cancelled")
        )
        self.assertTrue(wait_until(lambda: not pid_exists(manager._records[active_id].pid)))

    def test_concurrent_stop_and_natural_completion_is_terminal(self):
        script = self._script(
            "natural-race",
            "import json, time\n"
            "time.sleep(0.15)\n"
            "print(json.dumps({'status': 'SUCCESS', 'response': 'done'}))\n",
        )
        manager = self._manager(script)
        worker_id = self._spawn_batch(manager, 1)[0]
        self.assertTrue(wait_until(lambda: manager._records[worker_id].pid is not None))
        pid = manager._records[worker_id].pid
        self.test_groups.add(manager._records[worker_id].pgid)

        stopper = threading.Thread(target=lambda: (time.sleep(0.12), self._stop(manager, worker_id)))
        stopper.start()
        stopper.join(timeout=2)

        self.assertTrue(
            wait_until(lambda: manager._records[worker_id].status in TERMINAL_STATUSES)
        )
        self.assertIn(manager._records[worker_id].status, {"succeeded", "cancelled"})
        self.assertTrue(wait_until(lambda: not pid_exists(pid)))
        self.assertEqual(manager._processes, {})

    def test_concurrent_spawn_and_shutdown_leaves_no_queued_record(self):
        script = self._script("spawn-shutdown", "import time\ntime.sleep(5)\n")
        manager = self._manager(script, max_concurrent=2)
        responses = []
        spawner = threading.Thread(
            target=lambda: responses.append(
                json.loads(
                    manager.handle(
                        {
                            "action": "spawn",
                            "tasks": [
                                {"goal": f"race {index}", "workdir": self.temp_work.name}
                                for index in range(6)
                            ],
                        },
                        session_id="shutdown-session",
                    )
                )
            )
        )
        spawner.start()
        time.sleep(0.01)
        manager.shutdown()
        spawner.join(timeout=3)
        with manager._lock:
            self.test_groups.update(
                record.pgid for record in manager._records.values() if record.pgid
            )
            records = list(manager._records.values())

        self.assertFalse(spawner.is_alive())
        self.assertTrue(all(record.status in TERMINAL_STATUSES for record in records))
        self.assertTrue(all(record.status != "queued" for record in records))
        self.assertEqual(manager._processes, {})
        self.assertTrue(all(not pid_exists(record.pid) for record in records))

    def test_unload_cancels_active_and_queued_workers_deterministically(self):
        script = self._script("unload", "import time\nwhile True: time.sleep(1)\n")
        manager = self._manager(script)
        worker_ids = self._spawn_batch(manager, 3)
        self.assertTrue(wait_until(lambda: manager._records[worker_ids[0]].pid is not None))
        active_pid = manager._records[worker_ids[0]].pid
        self.test_groups.add(manager._records[worker_ids[0]].pgid)

        manager.shutdown()
        manager.shutdown()

        self.assertTrue(
            all(manager._records[worker_id].status == "cancelled" for worker_id in worker_ids)
        )
        self.assertTrue(
            all(manager._records[worker_id].finished_at is not None for worker_id in worker_ids)
        )
        self.assertTrue(wait_until(lambda: not pid_exists(active_pid)))
        self.assertEqual(manager._processes, {})

    def test_uncaught_worker_exception_is_terminal_without_wait(self):
        script = self._script("never-started", "raise SystemExit(99)\n")
        manager = self._manager(script)
        entered = threading.Event()

        def crash(_worker_id):
            entered.set()
            raise RuntimeError("injected supervisor crash")

        with patch.object(manager, "_run_worker", side_effect=crash):
            worker_id = self._spawn_batch(manager, 1)[0]
            self.assertTrue(entered.wait(timeout=2))

        self.assertTrue(
            wait_until(lambda: manager._records[worker_id].status == "failed")
        )
        record = manager._records[worker_id]
        self.assertIsNotNone(record.finished_at)
        self.assertIn("injected supervisor crash", record.error)
        self.assertTrue(wait_until(lambda: len(manager._futures) == 0))

    def test_500_fake_workers_leave_bounded_supervisor_state(self):
        pid_log = Path(self.temp_work.name) / "stress-pids.log"
        response_text = "x" * 2200
        script = self._script(
            "stress-worker",
            "import json, os\n"
            f"pid_log = {str(pid_log)!r}\n"
            "with open(pid_log, 'a') as handle: handle.write(str(os.getpid()) + '\\n')\n"
            f"print(json.dumps({{'status': 'SUCCESS', 'response': {response_text!r}}}))\n",
        )
        manager = AgyManager(
            settings={
                "max_concurrent": 10,
                "default_timeout_seconds": 60,
                "max_records_in_memory": 25,
                "max_raw_output_bytes": 4096,
                "retention_days": 30,
                "max_total_storage_mb": 1,
            },
            agy_path=str(script),
        )
        self.managers.append(manager)
        fd_dir = Path("/proc/self/fd")
        baseline_fds = len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None

        for wave in range(50):
            result = json.loads(
                manager.handle(
                    {
                        "action": "spawn",
                        "tasks": [
                            {
                                "goal": f"stress worker {wave}-{index}",
                                "workdir": self.temp_work.name,
                                "timeout_seconds": 60,
                            }
                            for index in range(10)
                        ],
                    },
                    session_id="shutdown-session",
                )
            )
            self.assertTrue(result["success"], result)

        self.assertTrue(wait_until(lambda: len(manager._futures) == 0, timeout=30))
        self.assertTrue(wait_until(lambda: len(manager._processes) == 0, timeout=5))
        self.assertTrue(_wait_for_lines(pid_log, 500, timeout=10))
        pids = [int(line) for line in pid_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(pids), 500)
        self.assertTrue(all(not pid_exists(pid) for pid in pids))
        self.assertLessEqual(len(manager._records), 25)
        runs_size = sum(
            path.stat().st_size for path in manager.runs_dir.iterdir() if path.is_file()
        )
        self.assertLessEqual(runs_size, 1024 * 1024)
        if baseline_fds is not None:
            self.assertLessEqual(len(list(fd_dir.iterdir())), baseline_fds + 8)

    def test_shutdown_holds_lease_while_future_is_alive_and_reaper_releases_it(self):
        tools_module.SHUTDOWN_WAIT_SECONDS = 0.1
        script = self._script("sleeper", "import time\nwhile True: time.sleep(1)\n")
        manager_a = self._manager(script, max_concurrent=2)

        release_worker = threading.Event()

        def gated_execute_once(record, prompt):
            release_worker.wait(timeout=5)
            return True

        with patch.object(manager_a, "_execute_once", side_effect=gated_execute_once):
            worker_id = self._spawn_batch(manager_a, 1)[0]
            self.assertTrue(wait_until(lambda: manager_a._records[worker_id].status == "running"))

            manager_a.shutdown()
            self.assertTrue(manager_a._closed)
            # The lease MUST still be held because the future is blocked in gated_execute_once
            self.assertIsNotNone(manager_a._runtime_lease_fd)

            # Starting manager_b in same HERMES_HOME must see manager_a's lease as locked
            manager_b = AgyManager(settings={"max_concurrent": 1}, agy_path=str(script))
            self.managers.append(manager_b)
            # Manager B must not have marked manager A's active record as interrupted
            self.assertNotIn(worker_id, manager_b._records)

            # Now release the blocked worker
            release_worker.set()

            # The lease must eventually be released by the reaper
            self.assertTrue(
                wait_until(lambda: manager_a._runtime_lease_fd is None, timeout=5),
                "reaper did not release the runtime lease",
            )
            self.assertFalse(manager_b._runtime_lease_is_locked(manager_a.runtime_id))

    def test_terminal_record_does_not_hide_live_future_from_shutdown_reaper(self):
        tools_module.SHUTDOWN_WAIT_SECONDS = 0.05
        script = self._script("terminal-future", "raise SystemExit(99)\n")
        manager_a = self._manager(script)
        entered = threading.Event()
        release_worker = threading.Event()

        def terminal_then_block(worker_id):
            with manager_a._lock:
                record = manager_a._records[worker_id]
                manager_a._transition(record, "succeeded", finished=True)
            entered.set()
            release_worker.wait(timeout=5)

        try:
            with patch.object(manager_a, "_run_worker", side_effect=terminal_then_block):
                worker_id = self._spawn_batch(manager_a, 1)[0]
                self.assertTrue(entered.wait(timeout=2))
                with manager_a._lock:
                    future = manager_a._futures[worker_id]

                manager_a.shutdown()

                self.assertFalse(future.done())
                self.assertIsNotNone(manager_a._runtime_lease_fd)
                manager_b = self._manager(script)
                self.assertTrue(manager_b._runtime_lease_is_locked(manager_a.runtime_id))

                release_worker.set()
                self.assertTrue(
                    wait_until(lambda: manager_a._runtime_lease_fd is None, timeout=5),
                    "reaper released neither future nor lease after supervisor completion",
                )
        finally:
            release_worker.set()


if __name__ == "__main__":
    unittest.main()
