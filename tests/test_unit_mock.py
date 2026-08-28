"""Comprehensive zero-quota unit mock test suite for delegate-agy plugin."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
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

    def test_build_command_flags(self):
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
        self.assertEqual(cmd[0], "/usr/bin/agy")
        self.assertIn("-p", cmd)
        self.assertIn("Prompt content", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIn("/tmp/test", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("gemini-3.7-flash-high", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--print-timeout", cmd)
        self.assertIn("120s", cmd)
        self.assertIn("--conversation", cmd)
        self.assertIn("conv_12345", cmd)
        self.assertIn("--effort", cmd)
        self.assertIn("high", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertNotIn("--sandbox", cmd)

        # Sandbox mode flags
        record.execution_mode = "sandbox"
        record.conversation_id = None
        record.effort = None
        cmd_sandbox = self.manager._build_command(record, record.prompt)
        self.assertIn("--sandbox", cmd_sandbox)
        self.assertIn("--dangerously-skip-permissions", cmd_sandbox)
        self.assertNotIn("--mode", cmd_sandbox)
        self.assertNotIn("--conversation", cmd_sandbox)

    def test_parse_payload_variations(self):
        # 1. Clean JSON
        payload1 = {"status": "SUCCESS", "response": "Done", "conversation_id": "c1"}
        self.assertEqual(
            self.manager._parse_payload(json.dumps(payload1)),
            payload1,
        )

        # 2. Leading logs before JSON line
        raw = "Fetching models...\nRunning print mode...\n" + json.dumps(payload1)
        self.assertEqual(self.manager._parse_payload(raw), payload1)

        # 3. Malformed JSON
        self.assertIsNone(self.manager._parse_payload("Not a JSON object"))
        self.assertIsNone(self.manager._parse_payload(""))

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
            self.assertIn("status ERROR", record.error)

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


if __name__ == "__main__":
    unittest.main()
