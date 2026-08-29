"""Comprehensive zero-quota unit mock test suite for delegate-agy plugin."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from schemas import DELEGATE_AGY_SCHEMA
from tools import AgyManager, RunRecord, register_tools, MODEL_BY_TASK_TYPE


class UnitMockTests(unittest.TestCase):
    def setUp(self):
        self.temp_home = tempfile.TemporaryDirectory(prefix="agy-unit-home-")
        self.temp_work = tempfile.TemporaryDirectory(prefix="agy-unit-work-")
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.temp_home.name})
        self.env_patch.start()
        self.getpgid_patch = patch("tools.os.getpgid", side_effect=lambda pid: pid)
        self.getpgrp_patch = patch("tools.os.getpgrp", return_value=999999)
        self.getpgid_patch.start()
        self.getpgrp_patch.start()
        self.manager = AgyManager(
            settings={
                "max_concurrent": 2,
                "default_timeout_seconds": 60,
                "max_result_chars": 5000,
            },
            agy_path="/usr/bin/agy",
        )

    def tearDown(self):
        self.manager.shutdown()
        self.getpgrp_patch.stop()
        self.getpgid_patch.stop()
        self.env_patch.stop()
        self.temp_home.cleanup()
        self.temp_work.cleanup()

    def test_schema_structure(self):
        self.assertEqual(DELEGATE_AGY_SCHEMA["name"], "delegate_agy")
        props = DELEGATE_AGY_SCHEMA["parameters"]["properties"]
        self.assertIn("action", props)
        self.assertIn("tasks", props)
        self.assertIn("execution_mode", props)
        self.assertEqual(props["execution_mode"]["enum"], ["sandbox", "accept_edits"])

    def test_task_spec_validation(self):
        # Valid investigation default
        spec = self.manager._task_spec(
            {"goal": "Check logs", "workdir": self.temp_work.name},
            default_workdir=None,
        )
        self.assertEqual(spec["task_type"], "investigation")
        self.assertEqual(spec["model"], MODEL_BY_TASK_TYPE["investigation"])
        self.assertEqual(spec["execution_mode"], "sandbox")

        # Explicit model override
        spec2 = self.manager._task_spec(
            {"goal": "Check logs", "model": "claude-sonnet-4-6", "workdir": self.temp_work.name},
            default_workdir=None,
        )
        self.assertEqual(spec2["model"], "claude-sonnet-4-6")

        # Missing goal raises
        with self.assertRaises(ValueError):
            self.manager._task_spec({"workdir": self.temp_work.name}, default_workdir=None)

        # Invalid model ID format raises
        with self.assertRaises(ValueError):
            self.manager._task_spec(
                {"goal": "test", "model": "bad model with spaces; rm -rf", "workdir": self.temp_work.name},
                default_workdir=None,
            )

        # Invalid execution_mode raises
        with self.assertRaises(ValueError):
            self.manager._task_spec(
                {"goal": "test", "execution_mode": "invalid_mode", "workdir": self.temp_work.name},
                default_workdir=None,
            )

    def test_build_command_exact_accept_edits_argv(self):
        record = RunRecord(
            worker_id="agy_test1",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Prompt content",
            task_type="implementation",
            model="gemini-3.7-flash-high",
            execution_mode="accept_edits",
            effort="high",
            workdir="/tmp/test",
            timeout_seconds=120,
            conversation_id="conv_12345",
        )
        cmd = self.manager._build_command(record, record.prompt)
        self.assertEqual(
            cmd,
            [
                "/usr/bin/agy",
                "-p",
                "Prompt content",
                "--add-dir",
                "/tmp/test",
                "--model",
                "gemini-3.7-flash-high",
                "--output-format",
                "json",
                "--print-timeout",
                "120s",
                "--conversation",
                "conv_12345",
                "--effort",
                "high",
                "--dangerously-skip-permissions",
            ],
        )

    def test_build_command_exact_sandbox_argv(self):
        record = RunRecord(
            worker_id="agy_test_sandbox",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Sandbox prompt",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir="/tmp/sandbox-test",
            timeout_seconds=90,
        )

        self.assertEqual(
            self.manager._build_command(record, record.prompt),
            [
                "/usr/bin/agy",
                "-p",
                "Sandbox prompt",
                "--add-dir",
                "/tmp/sandbox-test",
                "--model",
                "gemini-3.7-flash-low",
                "--output-format",
                "json",
                "--print-timeout",
                "90s",
                "--sandbox",
                "--dangerously-skip-permissions",
            ],
        )

    def test_parse_payload_variations(self):
        payload = {"status": "SUCCESS", "response": "Done", "conversation_id": "c1"}
        self.assertEqual(self.manager._parse_payload(json.dumps(payload)), payload)
        self.assertEqual(
            self.manager._parse_payload(f"Fetching models... {json.dumps(payload)}"),
            payload,
        )
        self.assertEqual(
            self.manager._parse_payload(
                "Fetching models...\n" + json.dumps(payload, indent=2)
            ),
            payload,
        )
        self.assertEqual(
            self.manager._parse_payload(json.dumps(payload) + "\nFinished cleanup"),
            payload,
        )
        self.assertIsNone(self.manager._parse_payload("Not a JSON object"))
        self.assertIsNone(self.manager._parse_payload(""))
        self.assertIsNone(self.manager._parse_payload(json.dumps([payload])))
        self.assertEqual(
            self.manager._parse_payload(
                "\n".join(
                    [
                        json.dumps({"level": "info", "message": "starting"}),
                        json.dumps(payload),
                        json.dumps({"event": "cleanup", "status_code": 0}),
                    ]
                )
            ),
            payload,
        )
        # M4: Decoy status-only log following a valid SUCCESS payload is ignored in favor of real result
        decoy_log = json.dumps({"status": "SUCCESS", "event": "cleanup-log"})
        self.assertEqual(
            self.manager._parse_payload(f"{json.dumps(payload)}\n{decoy_log}"),
            payload,
        )
        # Decoy log alone without response, conversation_id, or error returns None
        self.assertIsNone(self.manager._parse_payload(decoy_log))
        self.assertIsNone(self.manager._parse_payload(json.dumps({"status": "ERROR", "event": "err-event"})))
        # Valid ERROR with structured error payload is parsed
        error_payload = {"status": "ERROR", "error": "rate limit exceeded"}
        self.assertEqual(self.manager._parse_payload(json.dumps(error_payload)), error_payload)
        self.assertIsNone(
            self.manager._parse_payload(
                json.dumps({"status": "ERROR", "response": "JSON-shaped error log"})
            )
        )

    def test_execute_once_success_and_error_handling(self):
        record = RunRecord(
            worker_id="agy_test2",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Prompt content",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        self.manager._records[record.worker_id] = record

        # Mock Popen for Success
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps({
                "status": "SUCCESS",
                "response": "ALL_GOOD",
                "conversation_id": "conv_999",
                "duration_seconds": 1.5,
                "num_turns": 2,
                "usage": {"prompt_tokens": 10},
            }),
            "",
        )
        mock_proc.poll.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            res = self.manager._execute_once(record, record.prompt)
            self.assertTrue(res)
            self.assertEqual(record.conversation_id, "conv_999")
            self.assertEqual(record.response, "ALL_GOOD")
            self.assertIsNone(record.error)
            self.assertEqual(record.num_turns, 2)

        # Mock Popen for AGY Status ERROR with returncode 0
        mock_proc.communicate.return_value = (
            json.dumps({
                "status": "ERROR",
                "response": "",
                "error": "Model quota exhausted",
            }),
            "",
        )
        with patch("subprocess.Popen", return_value=mock_proc):
            res = self.manager._execute_once(record, record.prompt)
            self.assertFalse(res)
            self.assertIn("Model quota exhausted", record.error)

    def test_nonzero_returncode_overrides_success_payload(self):
        record = RunRecord(
            worker_id="agy_nonzero",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Prompt content",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        self.manager._records[record.worker_id] = record
        mock_proc = MagicMock()
        mock_proc.pid = 12346
        mock_proc.returncode = 7
        mock_proc.communicate.return_value = (
            json.dumps({"status": "SUCCESS", "response": "misleading success"}),
            "process failed",
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(self.manager._execute_once(record, record.prompt))

        self.assertIn("return code 7", record.error)

    def test_structured_error_precedes_stderr(self):
        record = RunRecord(
            worker_id="agy_structured_error",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Prompt content",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        self.manager._records[record.worker_id] = record
        mock_proc = MagicMock()
        mock_proc.pid = 12347
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps(
                {
                    "status": "ERROR",
                    "response": "",
                    "error": "structured quota diagnosis",
                }
            ),
            "generic stderr fallback",
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(self.manager._execute_once(record, record.prompt))

        self.assertIn("structured quota diagnosis", record.error)
        self.assertNotIn("generic stderr fallback", record.error)

    def test_payload_metrics_reject_booleans_and_non_finite_values(self):
        record = RunRecord(
            worker_id="agy_invalid_metrics",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Prompt content",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        self.manager._records[record.worker_id] = record
        mock_proc = MagicMock()
        mock_proc.pid = 12348
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps(
                {
                    "status": "SUCCESS",
                    "response": "forward compatible",
                    "usage": ["not", "a", "mapping"],
                    "duration_seconds": True,
                    "num_turns": float("nan"),
                    "future_contract_field": {"nested": "ignored"},
                }
            ),
            "",
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            self.assertTrue(self.manager._execute_once(record, record.prompt))

        self.assertEqual(record.response, "forward compatible")
        self.assertEqual(record.usage, {})
        self.assertIsNone(record.duration_seconds)
        self.assertIsNone(record.num_turns)

    def test_unknown_status_is_not_success(self):
        record = RunRecord(
            worker_id="agy_unknown_status",
            owner_id="session1",
            goal="Do task",
            context="",
            prompt="Prompt content",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        self.manager._records[record.worker_id] = record
        mock_proc = MagicMock()
        mock_proc.pid = 12349
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (
            json.dumps({"status": "PARTIAL", "response": "not complete"}),
            "",
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            self.assertFalse(self.manager._execute_once(record, record.prompt))

        self.assertIn("status PARTIAL", record.error)

    def test_session_isolation(self):
        record = RunRecord(
            worker_id="agy_isolated",
            owner_id="session_A",
            goal="Task A",
            context="",
            prompt="Prompt",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        self.manager._persist(record)

        # Session A can access
        self.assertEqual(self.manager._owned_record("session_A", "agy_isolated").worker_id, "agy_isolated")

        # Session B cannot access
        with self.assertRaises(ValueError):
            self.manager._owned_record("session_B", "agy_isolated")

    def test_load_existing_records_interrupted_marking(self):
        # Create a non-terminal record on disk
        rec_id = "agy_crashed"
        rec_path = self.manager.runs_dir / f"{rec_id}.json"
        raw_data = {
            "worker_id": rec_id,
            "owner_id": "session1",
            "goal": "Interrupted goal",
            "context": "",
            "prompt": "Prompt",
            "task_type": "simple",
            "model": "gemini-3.7-flash-low",
            "execution_mode": "sandbox",
            "effort": None,
            "workdir": self.temp_work.name,
            "timeout_seconds": 60,
            "status": "running",
            "created_at": "2026-08-28T00:00:00Z",
        }
        rec_path.write_text(json.dumps(raw_data), encoding="utf-8")

        # Create new manager instance that loads from disk
        mgr2 = AgyManager(
            settings={"max_concurrent": 1},
            agy_path="/usr/bin/agy",
        )
        try:
            loaded = mgr2._records.get(rec_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.status, "interrupted")
            self.assertIn("ended before this worker completed", loaded.error)
        finally:
            mgr2.shutdown()

    def test_register_tools_and_check_fn(self):
        ctx_mock = MagicMock()
        ctx_mock.get_config.side_effect = lambda k, d: d

        with patch("shutil.which", return_value="/usr/bin/agy"):
            register_tools(ctx_mock)
            ctx_mock.register_tool.assert_called_once()
            kw = ctx_mock.register_tool.call_args.kwargs
            self.assertEqual(kw["name"], "delegate_agy")
            self.assertEqual(kw["toolset"], "delegate_agy")
            self.assertTrue(kw["check_fn"]())

        # When agy is not in path
        with patch("shutil.which", return_value=None):
            self.assertFalse(kw["check_fn"]())

    def test_registration_rejection_shuts_down_constructed_manager(self):
        ctx_mock = MagicMock()
        ctx_mock.get_config.side_effect = lambda _key, default: default
        ctx_mock.register_tool.return_value = None
        constructed = MagicMock()

        with patch("tools.AgyManager", return_value=constructed):
            with self.assertRaises(RuntimeError):
                register_tools(ctx_mock)

        constructed.shutdown.assert_called_once()

    def test_parse_payload_handles_null_and_falsy_fields_correctly(self):
        # Null response with no conv_id is not treated as a valid result
        decoy_null = json.dumps({"status": "SUCCESS", "response": None, "conversation_id": None})
        self.assertIsNone(self.manager._parse_payload(decoy_null))

        # Numeric and empty string responses are preserved accurately
        record = RunRecord(
            worker_id="agy_numeric",
            owner_id="session1",
            goal="Numeric test",
            context="",
            prompt="Numeric",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
        )
        payload = {"status": "SUCCESS", "response": 0, "conversation_id": "c1"}
        self.assertTrue(
            self.manager._interpret_payload(
                record,
                payload,
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            )
        )
        self.assertEqual(record.response, "0")

    def test_group_exists_guards_non_positive_and_own_pgrp(self):
        self.assertFalse(self.manager._group_exists(None))
        self.assertFalse(self.manager._group_exists(0))
        self.assertFalse(self.manager._group_exists(-1))
        if os.name == "posix":
            self.assertFalse(self.manager._group_exists(os.getpgrp()))

    def test_lease_release_is_thread_safe_and_idempotent(self):
        threads = [
            threading.Thread(target=self.manager._release_runtime_lease)
            for _ in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertIsNone(self.manager._runtime_lease_fd)

    def test_wait_action_normalizes_worker_ids_and_rejects_invalid_types(self):
        record = RunRecord(
            worker_id="agy_wait_target",
            owner_id="session1",
            goal="Wait target",
            context="",
            prompt="Prompt",
            task_type="simple",
            model="gemini-3.7-flash-low",
            execution_mode="sandbox",
            effort=None,
            workdir=self.temp_work.name,
            timeout_seconds=60,
            status="succeeded",
        )
        self.manager._persist(record)

        # String passed to worker_ids is normalized
        res_str = json.loads(
            self.manager.handle(
                {"action": "wait", "worker_ids": record.worker_id, "wait_timeout_seconds": 5},
                session_id="session1",
            )
        )
        self.assertTrue(res_str["success"], res_str)
        self.assertEqual(len(res_str["results"]), 1)
        self.assertEqual(res_str["results"][0]["worker_id"], record.worker_id)

        # Invalid type passed to worker_ids raises clear error
        res_bad = json.loads(
            self.manager.handle(
                {"action": "wait", "worker_ids": 12345, "wait_timeout_seconds": 5},
                session_id="session1",
            )
        )
        self.assertFalse(res_bad["success"])
        self.assertIn("worker_ids must be a list of strings", res_bad["error"])


if __name__ == "__main__":
    unittest.main()
