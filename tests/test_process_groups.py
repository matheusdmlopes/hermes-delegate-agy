"""Process group lifecycle tests for delegate-agy."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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


class ProcessGroupTests(unittest.TestCase):
    def test_stop_uses_process_group_captured_at_spawn(self):
        with tempfile.TemporaryDirectory(prefix="delegate-agy-home-") as hermes_home:
            with tempfile.TemporaryDirectory(prefix="delegate-agy-work-") as workdir:
                with patch.dict(os.environ, {"HERMES_HOME": hermes_home}):
                    AgyManager = load_manager_class()
                    fake_agy = Path(workdir) / "agy"
                    fake_agy.write_text(
                        "#!/usr/bin/env python3\n"
                        "import time\n"
                        "time.sleep(60)\n",
                        encoding="utf-8",
                    )
                    fake_agy.chmod(0o755)
                    manager = AgyManager(
                        settings={"max_concurrent": 1, "default_timeout_seconds": 180},
                        agy_path=str(fake_agy),
                    )
                    try:
                        spawned = json.loads(
                            manager.handle(
                                {
                                    "action": "spawn",
                                    "goal": "Wait for cancellation",
                                    "task_type": "simple",
                                    "workdir": workdir,
                                    "timeout_seconds": 180,
                                },
                                task_id="process-group-test",
                            )
                        )
                        worker_id = spawned["workers"][0]["worker_id"]
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            with manager._lock:
                                record = manager._records[worker_id]
                                if record.pid is not None:
                                    break
                            time.sleep(0.05)
                        else:
                            self.fail("Worker process did not start")

                        self.assertEqual(record.pgid, record.pid)
                        tools_module = sys.modules[AgyManager.__module__]
                        with patch.object(
                            tools_module.os,
                            "getpgid",
                            side_effect=AssertionError("PGID was looked up during termination"),
                        ):
                            stopped = json.loads(
                                manager.handle(
                                    {"action": "stop", "worker_id": worker_id},
                                    task_id="process-group-test",
                                )
                            )
                        self.assertTrue(stopped["success"], stopped)
                    finally:
                        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
