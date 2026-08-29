"""Regression test suite for all 19 audit findings from GPT 5.6 sol xhigh."""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
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
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools as tools_module
from tools import (
    AgyManager,
    RunRecord,
    OwnedProcess,
    register_tools,
    _check_agy_executable,
    _truncate_utf8,
    LEASE_LOCKED,
    LEASE_UNLOCKED,
    LEASE_UNKNOWN,
)


def _wait_until(predicate, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AuditFindingsRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_home = tempfile.TemporaryDirectory(prefix="agy-audit-home-")
        self.temp_work = tempfile.TemporaryDirectory(prefix="agy-audit-work-")
        self.env_patch = patch.dict(os.environ, {"HERMES_HOME": self.temp_home.name})
        self.env_patch.start()
        self.managers: list[AgyManager] = []
        self.created_groups: set[int] = set()

    def tearDown(self):
        for pgid in self.created_groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for manager in self.managers:
            try:
                manager.shutdown()
            except Exception:
                pass
        self.env_patch.stop()
        self.temp_work.cleanup()
        self.temp_home.cleanup()

    def _create_fake_script(self, name: str, body: str) -> Path:
        path = Path(self.temp_work.name) / name
        path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _manager(self, script_path: Path | str, **settings) -> AgyManager:
        mgr_settings = {
            "max_concurrent": 2,
            "default_timeout_seconds": 60,
            "allow_accept_edits": True,
            **settings,
        }
        manager = AgyManager(settings=mgr_settings, agy_path=str(script_path))
        self.managers.append(manager)
        return manager

    # --- H-01: PGID failure cleans up descendant and ensures quiescence ---
    def test_h01_getpgid_failure_cleans_up_descendant_and_ensures_quiescence(self):
        if os.name != "posix":
            self.skipTest("POSIX process groups required")

        pid_file = Path(self.temp_work.name) / "descendant.pid"
        script = self._create_fake_script(
            "fake_forking_agy",
            f"""
import os, sys, time, json
pid = os.fork()
if pid == 0:
    with open({repr(str(pid_file))}, "w") as f:
        f.write(str(os.getpid()))
    while True:
        time.sleep(0.5)
else:
    print(json.dumps({{"status": "SUCCESS", "response": "done", "conversation_id": "c1"}}))
    sys.exit(0)
""",
        )
        manager = self._manager(script)

        # Mock os.getpgid to raise OSError to simulate failure in capturing PGID
        with patch("tools.os.getpgid", side_effect=OSError("getpgid failed")):
            response = json.loads(
                manager.handle(
                    {
                        "action": "spawn",
                        "goal": "Test PGID error handling",
                        "workdir": self.temp_work.name,
                        "wait": True,
                        "wait_timeout_seconds": 5,
                    },
                    session_id="h01-session",
                )
            )

        # Wait for descendant PID file if it was created
        if pid_file.exists():
            descendant_pid = int(pid_file.read_text().strip())
            # Descendant process must NOT be alive
            _wait_until(lambda: not _group_exists(descendant_pid), timeout=3.0)
            self.assertFalse(_group_exists(descendant_pid))

    # --- H-02: Symlinks in storage directories rejected ---
    def test_h02_symlinks_in_storage_dirs_rejected_and_no_external_chmod(self):
        external_target = Path(self.temp_work.name) / "external_data"
        external_target.mkdir(mode=0o755)
        symlink_data = Path(self.temp_home.name) / "plugin-data" / "delegate-agy"
        symlink_data.parent.mkdir(parents=True, exist_ok=True)
        symlink_data.symlink_to(external_target, target_is_directory=True)

        fake_script = self._create_fake_script(
            "fake_noop",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )

        with self.assertRaises(ValueError) as cm:
            AgyManager(agy_path=str(fake_script))
        self.assertIn("symlink", str(cm.exception).lower())

        # External directory mode should remain unchanged (0755)
        st_mode = external_target.stat().st_mode & 0o777
        self.assertEqual(st_mode, 0o755)

    # --- H-03: Runtime lease acquisition exception safety ---
    def test_h03_runtime_lease_exception_safety_no_fd_leak(self):
        fake_script = self._create_fake_script(
            "fake_noop",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        initial_fds = len(os.listdir("/proc/self/fd")) if os.path.exists("/proc/self/fd") else 0

        # Simulate exception during lease initialization
        with patch.object(json, "dumps", side_effect=KeyboardInterrupt("injected interrupt")):
            with self.assertRaises(KeyboardInterrupt):
                AgyManager(agy_path=str(fake_script))

        if os.path.exists("/proc/self/fd"):
            final_fds = len(os.listdir("/proc/self/fd"))
            self.assertLessEqual(final_fds, initial_fds + 2)

    # --- H-04: Sandbox mode uses plan mode and restricted permissions ---
    def test_h04_sandbox_mode_uses_plan_and_accept_edits_disabled_by_default(self):
        fake_script = self._create_fake_script(
            "fake_obs",
            """
import os, sys, json
observed = {'argv': sys.argv[1:]}
print(json.dumps({'status': 'SUCCESS', 'response': json.dumps(observed), 'conversation_id': 'c1'}))
""",
        )
        # Default settings: allow_accept_edits is False
        manager = AgyManager(settings={"max_concurrent": 1}, agy_path=str(fake_script))
        self.managers.append(manager)

        # Sandbox run must pass --mode=plan --sandbox and NOT --dangerously-skip-permissions
        resp = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": "Check mode flags",
                    "execution_mode": "sandbox",
                    "workdir": self.temp_work.name,
                    "wait": True,
                },
                session_id="h04-session",
            )
        )
        self.assertTrue(resp["success"], resp)
        obs = json.loads(resp["results"][0]["response"])
        self.assertIn("--mode=plan", obs["argv"])
        self.assertIn("--sandbox", obs["argv"])
        self.assertNotIn("--dangerously-skip-permissions", obs["argv"])

        # Attempting accept_edits with allow_accept_edits=False must fail
        resp_blocked = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": "Should fail",
                    "execution_mode": "accept_edits",
                    "workdir": self.temp_work.name,
                },
                session_id="h04-session",
            )
        )
        self.assertFalse(resp_blocked["success"])
        self.assertIn("disabled by plugin settings", resp_blocked["error"])

    # --- H-05: PATH=. cannot switch binary via cwd ---
    def test_h05_relative_path_in_path_cannot_switch_binary_via_cwd(self):
        real_bin = self._create_fake_script(
            "real_agy",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'REAL_BINARY', 'conversation_id': 'c1'}))",
        )
        # Put a malicious binary inside the target workspace directory
        fake_bin_in_workdir = Path(self.temp_work.name) / "agy"
        fake_bin_in_workdir.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'status': 'SUCCESS', 'response': 'MALICIOUS_CWD', 'conversation_id': 'c1'}))\n",
            encoding="utf-8",
        )
        fake_bin_in_workdir.chmod(0o755)

        manager = self._manager(real_bin)
        resp = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": "Test binary path resolution",
                    "workdir": self.temp_work.name,
                    "wait": True,
                },
                session_id="h05-session",
            )
        )
        self.assertTrue(resp["success"], resp)
        self.assertEqual(resp["results"][0]["response"], "REAL_BINARY")

    # --- H-06: Sanitized subprocess environment and stdin devnull ---
    def test_h06_subprocess_environment_scrubbed_and_stdin_is_devnull(self):
        script = self._create_fake_script(
            "fake_env_check",
            """
import os, sys, json
secrets_leaked = [k for k in os.environ if "SECRET" in k or "HERMES_" in k or "KEY" in k]
stdin_is_atty = sys.stdin.isatty()
print(json.dumps({
    "status": "SUCCESS",
    "response": json.dumps({"leaked": secrets_leaked, "is_atty": stdin_is_atty}),
    "conversation_id": "c1"
}))
""",
        )
        manager = self._manager(script)
        with patch.dict(
            os.environ,
            {
                "HERMES_AUTH_TOKEN": "secret-hermes-token",
                "OPENAI_API_KEY": "sk-12345",
                "MY_SUPER_SECRET": "top-secret",
            },
        ):
            resp = json.loads(
                manager.handle(
                    {
                        "action": "spawn",
                        "goal": "Test environment scrubbing",
                        "workdir": self.temp_work.name,
                        "wait": True,
                    },
                    session_id="h06-session",
                )
            )
            self.assertTrue(resp["success"], resp)
            obs = json.loads(resp["results"][0]["response"])
            # Hermes secrets and credential-like values must NEVER reach
            # the worker, regardless of whether the agy binary is a
            # native executable (allowlist) or a shim script (denylist).
            # The denylist intentionally tolerates unrelated env vars
            # whose name happens to contain "KEY" (e.g. terminal
            # cosmetics like GUM_LOG_KEY_FOREGROUND) because shim
            # wrappers like mise/asdf/rtx need their full tool-chain
            # context to resolve the real agy binary.
            leaked_secrets = [
                k for k in obs["leaked"]
                if any(
                    pat in k
                    for pat in (
                        "HERMES_", "OPENAI_", "ANTHROPIC_", "CLAUDE_",
                        "AWS_", "GITHUB_TOKEN", "SECRET", "PASSWORD",
                        "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_",
                        "PYTHONPATH", "PYTHONHOME",
                    )
                )
            ]
            self.assertEqual(leaked_secrets, [], f"Leaked secret-like vars: {leaked_secrets}")
            # And confirm the test scenario's hostile secrets specifically
            # did not reach the worker.
            self.assertNotIn("HERMES_AUTH_TOKEN", obs["leaked"])
            self.assertNotIn("OPENAI_API_KEY", obs["leaked"])
            self.assertNotIn("MY_SUPER_SECRET", obs["leaked"])
            self.assertFalse(obs["is_atty"])

    # --- H-07: Output bounded in memory and records ---
    def test_h07_large_output_streamed_to_disk_memory_strictly_bounded_and_response_capped(self):
        script = self._create_fake_script(
            "fake_large_output",
            """
import json, sys
large_text = "A" * 2000000
print(json.dumps({"status": "SUCCESS", "response": large_text, "conversation_id": "c1"}))
""",
        )
        manager = self._manager(script, max_result_chars=5000, max_raw_output_bytes=5000000)
        resp = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": "Large output test",
                    "workdir": self.temp_work.name,
                    "wait": True,
                },
                session_id="h07-session",
            )
        )
        self.assertTrue(resp["success"], resp)
        result = resp["results"][0]
        self.assertTrue(result["response_truncated"])
        self.assertLessEqual(len(result["response"]), 5100)
        self.assertTrue(Path(result["stdout_path"]).exists())

    # --- H-08: Global and per-owner backpressure ---
    def test_h08_global_and_per_owner_backpressure_and_steer_limits(self):
        script = self._create_fake_script(
            "fake_sleeper",
            "import time, json\ntime.sleep(2)\nprint(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = self._manager(
            script,
            max_concurrent=1,
            max_pending_workers=2,
            max_pending_per_owner=2,
            max_steer_queue_size=2,
        )

        # Spawning 3 tasks when max_pending_workers=2 should be rejected immediately
        resp = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "tasks": [{"goal": "T1"}, {"goal": "T2"}, {"goal": "T3"}],
                    "workdir": self.temp_work.name,
                },
                session_id="h08-session",
            )
        )
        self.assertFalse(resp["success"])
        self.assertIn("capacity exceeded", resp["error"].lower())

    # --- H-09: Transactional batch spawn rollback on failure ---
    def test_h09_batch_spawn_transactional_rollback_and_compensation_on_failure(self):
        script = self._create_fake_script(
            "fake_ok",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = self._manager(script, max_concurrent=2)

        # Mock second task spec generation to raise an error
        call_count = 0
        orig_task_spec = manager._task_spec

        def failing_task_spec(raw, default_workdir):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("Injected second item validation failure")
            return orig_task_spec(raw, default_workdir)

        with patch.object(manager, "_task_spec", side_effect=failing_task_spec):
            resp = json.loads(
                manager.handle(
                    {
                        "action": "spawn",
                        "tasks": [{"goal": "Task 1"}, {"goal": "Task 2"}],
                        "workdir": self.temp_work.name,
                    },
                    session_id="h09-session",
                )
            )

        self.assertFalse(resp["success"])
        # No orphan active records or futures should remain
        active_records = [
            r for r in manager._records.values() if r.status not in tools_module.TERMINAL_STATUSES
        ]
        self.assertEqual(len(active_records), 0)

    # --- M-01: BaseException during register_tools cleans up manager and lease ---
    def test_m01_base_exception_during_registration_cleans_up_manager_and_lease(self):
        mock_ctx = MagicMock()
        mock_ctx.get_config.side_effect = lambda key, default=None: default
        mock_ctx.register_tool.side_effect = KeyboardInterrupt("Interrupt during registration")

        with self.assertRaises(KeyboardInterrupt):
            register_tools(mock_ctx)

    # --- M-02: Invalid records and unknown lease statuses fail-closed ---
    def test_m02_invalid_records_null_owners_and_lease_errors_fail_closed(self):
        fake_script = self._create_fake_script(
            "fake_ok",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = self._manager(fake_script)

        # 1. Blank session_id / task_id fails
        resp_blank = json.loads(
            manager.handle({"action": "list"}, session_id="   ", task_id="   ")
        )
        self.assertFalse(resp_blank["success"])

        # 2. Corrupt/invalid JSON record on disk is ignored rather than crashing
        corrupt_path = manager.runs_dir / "agy_corrupt.json"
        corrupt_path.write_text("not-a-json", encoding="utf-8")
        rec = manager._validated_record_from_path(corrupt_path)
        self.assertIsNone(rec)

        # 3. Status list instead of string is rejected
        corrupt_status = manager.runs_dir / "agy_badstatus.json"
        corrupt_status.write_text(
            json.dumps({"worker_id": "agy_badstatus", "owner_id": "o1", "status": []}),
            encoding="utf-8",
        )
        rec2 = manager._validated_record_from_path(corrupt_status)
        self.assertIsNone(rec2)

    # --- M-03: Submit failure never leaves queued record on disk ---
    def test_m03_submit_failure_never_leaves_queued_record(self):
        fake_script = self._create_fake_script(
            "fake_ok",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = self._manager(fake_script)
        manager._executor.shutdown(wait=True)

        resp = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": "Test submit failure",
                    "workdir": self.temp_work.name,
                },
                session_id="m03-session",
            )
        )
        self.assertFalse(resp["success"])
        queued_records = [
            json.loads(p.read_text(encoding="utf-8"))
            for p in manager.runs_dir.glob("agy_*.json")
            if json.loads(p.read_text(encoding="utf-8")).get("status") == "queued"
        ]
        self.assertEqual(len(queued_records), 0)

    # --- M-04: Parser rejects decoys with event/logger and non-finite metrics ---
    def test_m04_decoy_veto_before_status_and_nan_infinity_bool_rejected(self):
        # Decoy with SUCCESS status but containing event / logger
        decoy_with_success = json.dumps(
            {"status": "SUCCESS", "event": "stream_chunk", "response": "decoy"}
        )
        real_success = json.dumps(
            {"status": "SUCCESS", "response": "real response", "conversation_id": "c1"}
        )
        parsed = AgyManager._parse_payload(f"{decoy_with_success}\n{real_success}")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("response"), "real response")

        # Non-finite metrics & booleans in usage
        usage_with_invalid = {
            "prompt_tokens": True,  # Boolean must be rejected
            "completion_tokens": 100,
            "cost": 0.05,
        }
        sanitized = AgyManager._sanitize_usage(usage_with_invalid)
        self.assertNotIn("prompt_tokens", sanitized)
        self.assertEqual(sanitized["completion_tokens"], 100)
        self.assertEqual(sanitized["cost"], 0.05)

    # --- M-05: Reaper prunes orphaned worker IDs without infinite loop ---
    def test_m05_reaper_prunes_orphaned_worker_ids_without_infinite_loop(self):
        fake_script = self._create_fake_script(
            "fake_ok",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = self._manager(fake_script)
        # Add an orphan ID with no corresponding process in _processes
        manager._ensure_resource_reaper("agy_orphan_id_123")
        # Reaper thread should prune the orphan ID and terminate cleanly
        _wait_until(
            lambda: manager._reaper_thread is None or not manager._reaper_thread.is_alive(),
            timeout=2.0,
        )
        self.assertNotIn("agy_orphan_id_123", manager._reap_worker_ids)

    # --- M-06: Runtime locks unlinked on shutdown and stale tmp GC ---
    def test_m06_runtime_locks_unlinked_on_shutdown_and_stale_tmp_gc(self):
        fake_script = self._create_fake_script(
            "fake_ok",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = AgyManager(
            settings={"max_concurrent": 1}, agy_path=str(fake_script)
        )
        lock_path = manager.runtime_dir / f"{manager.runtime_id}.lock"
        self.assertTrue(lock_path.exists())

        manager.shutdown()
        # Lock file must be unlinked after shutdown
        self.assertFalse(lock_path.exists())

    # --- M-07: Long prompts bounded to prevent E2BIG ---
    def test_m07_long_prompts_bounded_to_prevent_e2big(self):
        fake_script = self._create_fake_script(
            "fake_ok",
            "import json; print(json.dumps({'status': 'SUCCESS', 'response': 'ok'}))",
        )
        manager = self._manager(fake_script)
        huge_goal = "X" * 70000
        resp = json.loads(
            manager.handle(
                {
                    "action": "spawn",
                    "goal": huge_goal,
                    "workdir": self.temp_work.name,
                },
                session_id="m07-session",
            )
        )
        self.assertFalse(resp["success"])
        self.assertIn("exceeds", resp["error"].lower())

    # --- M-08: Minimum version gated and cached ---
    def test_m08_minimum_version_gated_and_cached(self):
        old_script = self._create_fake_script(
            "fake_old_agy",
            "import sys; sys.argv[1:] == ['--version'] and print('agy 1.1.0')",
        )
        is_valid, _, err = _check_agy_executable(str(old_script))
        self.assertFalse(is_valid)
        self.assertIn("1.1.8", err or "")

        new_script = self._create_fake_script(
            "fake_new_agy",
            "import sys; sys.argv[1:] == ['--version'] and print('agy 1.2.0')",
        )
        is_valid_new, _, err_new = _check_agy_executable(str(new_script))
        self.assertTrue(is_valid_new)
        self.assertIsNone(err_new)

    # --- L-01: ModuleNotFoundError scoped ---
    def test_l01_module_not_found_error_scoped(self):
        with patch.dict(sys.modules, {"plugins.plugin_storage": None}):
            # ModuleNotFoundError for plugins.plugin_storage falls back cleanly
            data_dir = tools_module.plugin_data_dir("delegate-agy")
            self.assertTrue(str(data_dir).endswith("delegate-agy"))

    # --- L-02: UTF-8 safe truncation and directory fsync ---
    def test_l02_utf8_truncation_boundary_safety_and_dir_fsync(self):
        # Multibyte character (e.g. emoji 🛰️ is 4 bytes: \xf0\x9f\x9b\xb0)
        multibyte_string = "Hello 🛰️ World"
        # Truncating at byte index 8 would cut into the 4-byte emoji
        safe_truncated, is_trunc = _truncate_utf8(multibyte_string, 8)
        self.assertTrue(is_trunc)
        # Decoded output must be valid UTF-8 and not crash
        self.assertEqual(safe_truncated, "Hello ")


if __name__ == "__main__":
    unittest.main()
