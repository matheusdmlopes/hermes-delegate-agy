"""Multi-process persistence tests with local fake agy executables only."""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from tools import AgyManager, RunRecord


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _live_runtime(home: str, workdir: str, fake_agy: str, queue, release) -> None:
    os.environ["HERMES_HOME"] = home
    manager = AgyManager(
        settings={"max_concurrent": 1, "default_timeout_seconds": 180},
        agy_path=fake_agy,
    )
    try:
        response = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": "Remain active for persistence testing",
                    "workdir": workdir,
                    "timeout_seconds": 180,
                },
                session_id="shared-session",
            )
        )
        worker_id = response["workers"][0]["worker_id"]
        if not _wait_until(lambda: manager._records[worker_id].pid is not None):
            raise RuntimeError("fake worker did not start")
        record = manager._records[worker_id]
        queue.put(
            {
                "worker_id": worker_id,
                "runtime_id": manager.runtime_id,
                "worker_pid": record.pid,
                "worker_pgid": record.pgid,
            }
        )
        release.wait(30)
    except BaseException as exc:
        queue.put({"error": repr(exc)})
    finally:
        manager.shutdown()


def _atomic_writer(
    home: str,
    workdir: str,
    marker: str,
    barrier,
    queue,
) -> None:
    os.environ["HERMES_HOME"] = home
    manager = AgyManager(settings={"max_concurrent": 1}, agy_path="/fake/agy")
    record = RunRecord(
        worker_id="agy_shared_atomic",
        owner_id="shared-session",
        goal="atomic",
        context="",
        prompt="atomic",
        task_type="simple",
        model="gemini-3.7-flash-low",
        execution_mode="sandbox",
        effort=None,
        workdir=workdir,
        timeout_seconds=60,
        status="succeeded",
        runtime_id=manager.runtime_id,
        supervisor_pid=os.getpid(),
        supervisor_started_at=manager.supervisor_started_at,
    )
    try:
        barrier.wait(timeout=10)
        for index in range(100):
            record.response = f"{marker}:{index}"
            manager._write_record(record)
        queue.put(None)
    except BaseException as exc:
        queue.put(repr(exc))
    finally:
        manager.shutdown()


class PersistenceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("runtime leases require fcntl on POSIX")
        self.temp_home = tempfile.TemporaryDirectory(prefix="agy-persist-home-")
        self.temp_work = tempfile.TemporaryDirectory(prefix="agy-persist-work-")
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.temp_home.name})
        self.env_patch.start()
        self.fake_agy = Path(self.temp_work.name) / "fake-agy-sleeper"
        self.fake_agy.write_text(
            "#!/usr/bin/env python3\nimport time\nwhile True: time.sleep(1)\n",
            encoding="utf-8",
        )
        self.fake_agy.chmod(0o755)
        self.ctx = multiprocessing.get_context("spawn")
        self.children = []
        self.worker_groups: set[int] = set()

    def tearDown(self):
        for child in self.children:
            if child.is_alive():
                child.terminate()
            child.join(timeout=5)
            if child.is_alive():
                child.kill()
                child.join(timeout=5)
        for pgid in self.worker_groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.env_patch.stop()
        self.temp_work.cleanup()
        self.temp_home.cleanup()

    def _start_live_runtime(self):
        queue = self.ctx.Queue()
        release = self.ctx.Event()
        child = self.ctx.Process(
            target=_live_runtime,
            args=(
                self.temp_home.name,
                self.temp_work.name,
                str(self.fake_agy),
                queue,
                release,
            ),
        )
        child.start()
        self.children.append(child)
        info = queue.get(timeout=15)
        self.assertNotIn("error", info, info)
        self.worker_groups.add(info["worker_pgid"])
        return child, release, info

    def test_second_runtime_does_not_interrupt_live_foreign_record(self):
        child, release, info = self._start_live_runtime()
        record_path = (
            Path(self.temp_home.name)
            / "plugin-data"
            / "delegate-agy"
            / "runs"
            / f"{info['worker_id']}.json"
        )
        self.assertTrue(_wait_until(record_path.exists))

        manager_b = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        try:
            durable = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertIn(durable["status"], {"queued", "running"})
            self.assertEqual(durable["runtime_id"], info["runtime_id"])
            self.assertNotIn(info["worker_id"], manager_b._records)
        finally:
            manager_b.shutdown()
            release.set()
            child.join(timeout=10)
        self.assertEqual(child.exitcode, 0)

    def test_atomic_writes_from_two_processes_do_not_collide_or_leave_temps(self):
        queue = self.ctx.Queue()
        barrier = self.ctx.Barrier(2)
        children = [
            self.ctx.Process(
                target=_atomic_writer,
                args=(self.temp_home.name, self.temp_work.name, marker, barrier, queue),
            )
            for marker in ("alpha", "beta")
        ]
        self.children.extend(children)
        for child in children:
            child.start()
        errors = [queue.get(timeout=20), queue.get(timeout=20)]
        for child in children:
            child.join(timeout=10)

        self.assertEqual(errors, [None, None])
        self.assertTrue(all(child.exitcode == 0 for child in children))
        runs_dir = Path(self.temp_home.name) / "plugin-data" / "delegate-agy" / "runs"
        durable = json.loads((runs_dir / "agy_shared_atomic.json").read_text(encoding="utf-8"))
        self.assertIn(durable["response"], {"alpha:99", "beta:99"})
        self.assertEqual(list(runs_dir.glob("*.tmp")), [])
        self.assertEqual(stat.S_IMODE((runs_dir / "agy_shared_atomic.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(runs_dir.stat().st_mode), 0o700)
        runtime_dir = runs_dir.parent / "runtime"
        self.assertEqual(stat.S_IMODE(runtime_dir.stat().st_mode), 0o700)

    def test_unclean_runtime_is_recovered_as_interrupted(self):
        child, _release, info = self._start_live_runtime()
        child.terminate()
        child.join(timeout=10)
        self.assertIsNotNone(child.exitcode)

        recovered = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        try:
            record = recovered._records[info["worker_id"]]
            self.assertEqual(record.status, "interrupted")
            self.assertIsNotNone(record.finished_at)
        finally:
            recovered.shutdown()

    def test_terminal_and_legacy_records_load_tolerantly(self):
        runs_dir = Path(self.temp_home.name) / "plugin-data" / "delegate-agy" / "runs"
        runs_dir.mkdir(parents=True)
        base = {
            "owner_id": "legacy-session",
            "goal": "legacy",
            "context": "",
            "prompt": "legacy",
            "task_type": "simple",
            "model": "gemini-3.7-flash-low",
            "execution_mode": "sandbox",
            "effort": None,
            "workdir": self.temp_work.name,
            "timeout_seconds": 60,
            "created_at": "2026-08-28T00:00:00+00:00",
            "future_unknown_field": "ignored",
        }
        terminal = {**base, "worker_id": "agy_legacy_terminal", "status": "succeeded"}
        active = {**base, "worker_id": "agy_legacy_active", "status": "running"}
        (runs_dir / "agy_legacy_terminal.json").write_text(json.dumps(terminal), encoding="utf-8")
        (runs_dir / "agy_legacy_active.json").write_text(json.dumps(active), encoding="utf-8")

        manager = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        try:
            self.assertEqual(manager._records["agy_legacy_terminal"].status, "succeeded")
            self.assertEqual(manager._records["agy_legacy_active"].status, "interrupted")
            self.assertIsNone(manager._records["agy_legacy_terminal"].runtime_id)
        finally:
            manager.shutdown()

    def test_path_traversal_and_mismatched_worker_ids_are_rejected(self):
        runs_dir = Path(self.temp_home.name) / "plugin-data" / "delegate-agy" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        escaped_payload = {
            "worker_id": "../../escaped-record",
            "owner_id": "bad-session",
            "goal": "escape",
            "context": "",
            "prompt": "escape",
            "task_type": "simple",
            "model": "gemini-3.7-flash-low",
            "execution_mode": "sandbox",
            "effort": None,
            "workdir": self.temp_work.name,
            "timeout_seconds": 60,
            "status": "running",
            "runtime_id": None,
        }
        (runs_dir / "agy_seed.json").write_text(json.dumps(escaped_payload), encoding="utf-8")

        mismatched_payload = {
            "worker_id": "agy_different_worker",
            "owner_id": "bad-session",
            "goal": "mismatch",
            "context": "",
            "prompt": "mismatch",
            "task_type": "simple",
            "model": "gemini-3.7-flash-low",
            "execution_mode": "sandbox",
            "effort": None,
            "workdir": self.temp_work.name,
            "timeout_seconds": 60,
            "status": "running",
            "runtime_id": None,
        }
        (runs_dir / "agy_mismatch.json").write_text(json.dumps(mismatched_payload), encoding="utf-8")

        malformed_runtime_payload = {
            **mismatched_payload,
            "worker_id": "agy_malformed_runtime",
            "runtime_id": "not-a-32-character-hex-runtime",
        }
        malformed_runtime_path = runs_dir / "agy_malformed_runtime.json"
        malformed_runtime_path.write_text(
            json.dumps(malformed_runtime_payload),
            encoding="utf-8",
        )

        manager = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        try:
            # Traversal record must not have escaped to plugin-data/
            escaped_file = Path(self.temp_home.name) / "plugin-data" / "delegate-agy" / "escaped-record.json"
            self.assertFalse(escaped_file.exists())
            self.assertNotIn("../../escaped-record", manager._records)
            self.assertNotIn("agy_seed", manager._records)
            self.assertNotIn("agy_different_worker", manager._records)
            self.assertNotIn("agy_mismatch", manager._records)
            self.assertNotIn("agy_malformed_runtime", manager._records)
            self.assertEqual(
                json.loads(malformed_runtime_path.read_text(encoding="utf-8"))["status"],
                "running",
                "invalid runtime record must be ignored rather than rewritten",
            )
            self.assertFalse(manager._runtime_lease_is_locked("x"))

            with self.assertRaises(ValueError):
                manager._record_path("../../escaped-record")

            self.assertIsNone(manager._read_record("../../escaped-record"))
        finally:
            manager.shutdown()

    def test_constructor_failure_releases_lease_fd(self):
        fd_dir = Path("/proc/self/fd")
        has_proc_fd = fd_dir.is_dir()
        baseline_fds = set(os.listdir(fd_dir)) if has_proc_fd else None

        with patch.object(AgyManager, "_load_existing_records", side_effect=OSError("injected startup recovery failure")):
            with self.assertRaises(OSError):
                AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))

        if has_proc_fd:
            current_fds = set(os.listdir(fd_dir))
            # No leaked file descriptors
            self.assertLessEqual(len(current_fds - baseline_fds), 1)

        # A new manager can start up and acquire lease without locking conflict
        manager = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        manager.shutdown()

    def test_baseexception_during_lease_acquisition_closes_local_fd(self):
        with patch("tools.os.write", side_effect=KeyboardInterrupt("injected acquire interrupt")):
            with self.assertRaises(KeyboardInterrupt):
                AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))

        lock_fds = []
        fd_dir = Path("/proc/self/fd")
        if fd_dir.is_dir():
            for fd_path in fd_dir.iterdir():
                try:
                    target = os.readlink(fd_path)
                except OSError:
                    continue
                if target.startswith(self.temp_home.name) and target.endswith(".lock"):
                    lock_fds.append((int(fd_path.name), target))
        try:
            self.assertEqual(lock_fds, [])
        finally:
            for fd, _target in lock_fds:
                os.close(fd)

    def test_initial_persistence_failure_leaves_no_queued_orphan(self):
        manager = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        try:
            with patch.object(manager, "_write_record", side_effect=OSError("injected initial disk failure")):
                response = json.loads(
                    manager.handle(
                        {
                            "action": "spawn",
                            "goal": "Test initial failure",
                            "workdir": self.temp_work.name,
                        },
                        session_id="shared-session",
                    )
                )
                self.assertFalse(response["success"])
                self.assertIn("persistence", response["error"].lower())

            # No orphaned queued record in _records without a future
            self.assertEqual(len(manager._records), 0)
            self.assertEqual(len(manager._futures), 0)
        finally:
            manager.shutdown()

        runs_dir = Path(self.temp_home.name) / "plugin-data" / "delegate-agy" / "runs"
        queued_files = [
            p for p in runs_dir.glob("agy_*.json")
            if json.loads(p.read_text(encoding="utf-8")).get("status") == "queued"
        ]
        self.assertEqual(queued_files, [])

    def test_post_replace_initial_persistence_failure_removes_published_json(self):
        manager = AgyManager(settings={"max_concurrent": 1}, agy_path=str(self.fake_agy))
        original_chmod = os.chmod

        def fail_record_chmod(path, mode):
            if str(path).endswith(".json"):
                raise OSError("injected chmod failure after replace")
            return original_chmod(path, mode)

        try:
            with patch("tools.os.chmod", side_effect=fail_record_chmod):
                response = json.loads(
                    manager.handle(
                        {
                            "action": "spawn",
                            "goal": "Test post-replace failure",
                            "workdir": self.temp_work.name,
                        },
                        session_id="shared-session",
                    )
                )
            self.assertFalse(response["success"])
            self.assertEqual(manager._records, {})
            self.assertEqual(manager._futures, {})
            self.assertEqual(list(manager.runs_dir.glob("agy_*.json")), [])
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
