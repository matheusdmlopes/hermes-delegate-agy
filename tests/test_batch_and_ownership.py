"""Behavioral regressions for batch defaults and stable session ownership."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from tools import AgyManager, RunRecord


class BatchAndOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp_home = tempfile.TemporaryDirectory(prefix="agy-batch-home-")
        self.temp_work = tempfile.TemporaryDirectory(prefix="agy-batch-work-")
        self.override_work = tempfile.TemporaryDirectory(prefix="agy-item-work-")
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.temp_home.name})
        self.env_patch.start()
        self.fake_agy = Path(self.temp_work.name) / "fake-agy"
        self.fake_agy.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "observed = {'argv': sys.argv[1:], 'cwd': os.getcwd()}\n"
            "print(json.dumps({'status': 'SUCCESS', 'response': json.dumps(observed), "
            "'conversation_id': 'fake-conversation'}))\n",
            encoding="utf-8",
        )
        self.fake_agy.chmod(0o755)
        self.manager = AgyManager(
            settings={
                "max_concurrent": 2,
                "default_timeout_seconds": 60,
                "allow_accept_edits": True,
            },
            agy_path=str(self.fake_agy),
        )

    def tearDown(self):
        self.manager.shutdown()
        self.env_patch.stop()
        self.override_work.cleanup()
        self.temp_work.cleanup()
        self.temp_home.cleanup()

    def _terminal_record(self, worker_id: str, owner_id: str) -> RunRecord:
        record = RunRecord(
            worker_id=worker_id,
            owner_id=owner_id,
            goal="Ownership test",
            context="",
            prompt="Ownership prompt",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
            status="succeeded",
        )
        self.manager._persist(record)
        return record

    def test_same_session_can_access_worker_when_task_id_changes(self):
        record = self._terminal_record("agy_stable_owner", "stable-session")

        result = json.loads(
            self.manager.handle(
                {"action": "status", "worker_id": record.worker_id},
                session_id="stable-session",
                task_id="new-turn-task-id",
            )
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["worker"]["worker_id"], record.worker_id)

    def test_different_session_is_rejected_even_with_matching_task_id(self):
        record = self._terminal_record("agy_cross_session", "stable-session")

        result = json.loads(
            self.manager.handle(
                {"action": "result", "worker_id": record.worker_id},
                session_id="intruder-session",
                task_id="stable-session",
            )
        )

        self.assertFalse(result["success"], result)
        self.assertIn("Worker not found in this session", result["error"])

    def test_missing_session_and_task_identifiers_fails_closed(self):
        result = json.loads(self.manager.handle({"action": "list"}))

        self.assertFalse(result["success"], result)
        self.assertIn("refusing unscoped access", result["error"])

    def test_batch_inherits_every_common_field(self):
        response = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "tasks": [{"goal": "Inherited goal"}],
                    "context": "inherited context marker",
                    "task_type": "implementation",
                    "model": "common-model-high",
                    "execution_mode": "accept_edits",
                    "effort": "high",
                    "workdir": self.temp_work.name,
                    "timeout_seconds": 91,
                    "wait": True,
                    "wait_timeout_seconds": 10,
                },
                session_id="batch-session",
                task_id="batch-turn-one",
            )
        )

        self.assertTrue(response["success"], response)
        result = response["results"][0]
        observed = json.loads(result["response"])
        self.assertEqual(result["task_type"], "implementation")
        self.assertEqual(result["model"], "common-model-high")
        self.assertEqual(result["execution_mode"], "accept_edits")
        self.assertEqual(observed["cwd"], str(Path(self.temp_work.name).resolve()))
        self.assertIn("inherited context marker", observed["argv"][1])
        self.assertIn("--effort", observed["argv"])
        self.assertEqual(observed["argv"][observed["argv"].index("--effort") + 1], "high")
        self.assertEqual(
            observed["argv"][observed["argv"].index("--print-timeout") + 1],
            "91s",
        )

    def test_item_fields_override_every_batch_default(self):
        response = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "tasks": [
                        {
                            "goal": "Override goal",
                            "context": "item context marker",
                            "task_type": "simple",
                            "model": "item-model-low",
                            "execution_mode": "sandbox",
                            "effort": "low",
                            "workdir": self.override_work.name,
                            "timeout_seconds": 92,
                        }
                    ],
                    "context": "batch context marker",
                    "task_type": "hard",
                    "model": "batch-model-high",
                    "execution_mode": "accept_edits",
                    "effort": "high",
                    "workdir": self.temp_work.name,
                    "timeout_seconds": 91,
                    "wait": True,
                    "wait_timeout_seconds": 10,
                },
                session_id="batch-session",
                task_id="batch-turn-two",
            )
        )

        self.assertTrue(response["success"], response)
        result = response["results"][0]
        observed = json.loads(result["response"])
        self.assertEqual(result["task_type"], "simple")
        self.assertEqual(result["model"], "item-model-low")
        self.assertEqual(result["execution_mode"], "sandbox")
        self.assertEqual(observed["cwd"], str(Path(self.override_work.name).resolve()))
        self.assertIn("item context marker", observed["argv"][1])
        self.assertNotIn("batch context marker", observed["argv"][1])
        self.assertEqual(observed["argv"][observed["argv"].index("--effort") + 1], "low")
        self.assertEqual(
            observed["argv"][observed["argv"].index("--print-timeout") + 1],
            "92s",
        )
        self.assertIn("--sandbox", observed["argv"])
        self.assertIn("--mode=plan", observed["argv"])

    def test_batch_rejects_non_mapping_items_clearly(self):
        response = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "tasks": ["not-a-task-mapping"],
                    "workdir": self.temp_work.name,
                },
                session_id="batch-session",
            )
        )

        self.assertFalse(response["success"], response)
        self.assertIn("mapping", response["error"])

    def test_submission_failure_never_leaves_durable_queued_record(self):
        self.manager._executor.shutdown(wait=True, cancel_futures=True)

        response = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "goal": "Cannot be submitted",
                    "workdir": self.temp_work.name,
                },
                session_id="submission-session",
            )
        )

        self.assertFalse(response["success"], response)
        record_paths = list(self.manager.runs_dir.glob("agy_*.json"))
        self.assertEqual(len(record_paths), 1)
        durable = json.loads(record_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(durable["status"], "failed")
        self.assertIsNotNone(durable["finished_at"])
        self.assertIn("submit", durable["error"].lower())

    def test_old_terminal_worker_reloads_after_memory_eviction(self):
        self.manager.shutdown()
        self.manager = AgyManager(
            settings={
                "max_concurrent": 2,
                "default_timeout_seconds": 60,
                "max_records_in_memory": 2,
            },
            agy_path=str(self.fake_agy),
        )
        worker_ids = []
        for index in range(4):
            response = json.loads(
                self.manager.handle(
                    {
                        "action": "spawn",
                        "goal": f"terminal record {index}",
                        "workdir": self.temp_work.name,
                        "wait": True,
                        "wait_timeout_seconds": 10,
                    },
                    session_id="cache-session",
                )
            )
            self.assertTrue(response["success"], response)
            worker_ids.append(response["results"][0]["worker_id"])

        self.assertLessEqual(len(self.manager._records), 2)
        old_result = json.loads(
            self.manager.handle(
                {"action": "result", "worker_id": worker_ids[0]},
                session_id="cache-session",
                task_id="later-turn",
            )
        )
        self.assertTrue(old_result["success"], old_result)
        self.assertEqual(old_result["worker"]["worker_id"], worker_ids[0])

    def test_raw_output_respects_configured_byte_bound(self):
        self.manager.shutdown()
        self.manager = AgyManager(
            settings={
                "max_concurrent": 1,
                "default_timeout_seconds": 60,
                "max_raw_output_bytes": 128,
            },
            agy_path=str(self.fake_agy),
        )
        response = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "goal": "bounded output",
                    "workdir": self.temp_work.name,
                    "wait": True,
                    "wait_timeout_seconds": 10,
                },
                session_id="raw-session",
            )
        )

        result = response["results"][0]
        self.assertEqual(result["status"], "succeeded")
        self.assertLessEqual(Path(result["stdout_path"]).stat().st_size, 128)

    def test_steering_preserves_attempt_specific_raw_output(self):
        steering_agy = Path(self.temp_work.name) / "steering-agy"
        steering_agy.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "continued = '--conversation' in sys.argv\n"
            "if not continued: time.sleep(0.15)\n"
            "response = 'second-attempt' if continued else 'first-attempt'\n"
            "print(json.dumps({'status': 'SUCCESS', 'response': response, "
            "'conversation_id': 'conversation-for-steering'}))\n",
            encoding="utf-8",
        )
        steering_agy.chmod(0o755)
        self.manager.shutdown()
        self.manager = AgyManager(
            settings={"max_concurrent": 1, "default_timeout_seconds": 60},
            agy_path=str(steering_agy),
        )
        spawned = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "goal": "first turn",
                    "workdir": self.temp_work.name,
                },
                session_id="steer-session",
            )
        )
        worker_id = spawned["workers"][0]["worker_id"]
        steered = json.loads(
            self.manager.handle(
                {
                    "action": "steer",
                    "worker_id": worker_id,
                    "message": "second turn",
                },
                session_id="steer-session",
            )
        )
        self.assertTrue(steered["success"], steered)
        waited = json.loads(
            self.manager.handle(
                {
                    "action": "wait",
                    "worker_id": worker_id,
                    "wait_timeout_seconds": 10,
                },
                session_id="steer-session",
            )
        )
        self.assertEqual(waited["results"][0]["status"], "succeeded")
        first_stdout = self.manager.runs_dir / f"{worker_id}.turn-001.stdout"
        second_stdout = self.manager.runs_dir / f"{worker_id}.turn-002.stdout"
        self.assertIn("first-attempt", first_stdout.read_text(encoding="utf-8"))
        self.assertIn("second-attempt", second_stdout.read_text(encoding="utf-8"))
        self.assertNotEqual(first_stdout, second_stdout)

    def test_accept_edits_can_be_disabled_by_plugin_setting(self):
        self.manager.shutdown()
        self.manager = AgyManager(
            settings={"allow_accept_edits": False},
            agy_path=str(self.fake_agy),
        )

        response = json.loads(
            self.manager.handle(
                {
                    "action": "spawn",
                    "goal": "must be rejected",
                    "execution_mode": "accept_edits",
                    "workdir": self.temp_work.name,
                },
                session_id="safe-session",
            )
        )

        self.assertFalse(response["success"], response)
        self.assertIn("disabled", response["error"])
        self.assertEqual(self.manager._records, {})


if __name__ == "__main__":
    unittest.main()
