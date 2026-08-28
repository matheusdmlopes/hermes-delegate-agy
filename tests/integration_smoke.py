"""Real smoke test for the local delegate-agy plugin.

This intentionally consumes a small amount of Antigravity quota.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "delegate_agy_local"


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


def call(manager, payload):
    result = json.loads(manager.handle(payload, task_id="integration-smoke"))
    if not result.get("success"):
        raise AssertionError(result)
    return result


def wait_for_status(manager, worker_id, wanted, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = call(manager, {"action": "status", "worker_id": worker_id})["worker"]
        if last["status"] in wanted:
            return last
        time.sleep(0.1)
    raise AssertionError(f"Worker {worker_id} did not reach {wanted}: {last}")


def main():
    with tempfile.TemporaryDirectory(prefix="delegate-agy-home-") as hermes_home:
        with tempfile.TemporaryDirectory(prefix="delegate-agy-work-") as workdir:
            os.environ["HERMES_HOME"] = hermes_home
            AgyManager = load_manager_class()
            manager = AgyManager(
                settings={
                    "max_concurrent": 3,
                    "default_timeout_seconds": 180,
                    "max_result_chars": 24000,
                }
            )
            summary = {}
            try:
                unscoped = json.loads(manager.handle({"action": "list"}))
                assert not unscoped.get("success"), unscoped
                assert "refusing unscoped access" in unscoped.get("error", ""), unscoped

                batch = call(
                    manager,
                    {
                        "action": "spawn",
                        "tasks": [
                            {
                                "goal": "Respond exactly: WORKER_ALPHA",
                                "task_type": "simple",
                                "workdir": workdir,
                                "timeout_seconds": 180,
                            },
                            {
                                "goal": "Respond exactly: WORKER_BETA",
                                "task_type": "simple",
                                "workdir": workdir,
                                "timeout_seconds": 180,
                            },
                        ],
                        "wait": True,
                        "wait_timeout_seconds": 300,
                    },
                )
                assert len(batch["results"]) == 2, batch
                assert all(item["status"] == "succeeded" for item in batch["results"]), batch
                assert "WORKER_ALPHA" in batch["results"][0]["response"], batch
                assert "WORKER_BETA" in batch["results"][1]["response"], batch
                assert all(item["model"] == "gemini-3.7-flash-low" for item in batch["results"])
                summary["parallel"] = [
                    {
                        "worker_id": item["worker_id"],
                        "model": item["model"],
                        "conversation_id": item["conversation_id"],
                        "response": item["response"],
                    }
                    for item in batch["results"]
                ]

                source_id = batch["results"][0]["worker_id"]
                continued = call(
                    manager,
                    {
                        "action": "continue",
                        "worker_id": source_id,
                        "message": "Respond exactly: CONTINUED_ALPHA",
                        "task_type": "investigation",
                        "wait": True,
                        "wait_timeout_seconds": 240,
                    },
                )["result"]
                assert continued["status"] == "succeeded", continued
                assert continued["parent_worker_id"] == source_id, continued
                assert "CONTINUED_ALPHA" in continued["response"], continued
                assert continued["conversation_id"] == batch["results"][0]["conversation_id"], continued
                assert continued["model"] == "gemini-3.7-flash-medium", continued
                summary["continue"] = {
                    "worker_id": continued["worker_id"],
                    "parent_worker_id": continued["parent_worker_id"],
                    "conversation_id": continued["conversation_id"],
                    "response": continued["response"],
                }

                steered_spawn = call(
                    manager,
                    {
                        "action": "spawn",
                        "goal": "Use the terminal to run sleep 3, then respond exactly: FIRST_PASS",
                        "task_type": "simple",
                        "workdir": workdir,
                        "timeout_seconds": 180,
                    },
                )
                steered_id = steered_spawn["workers"][0]["worker_id"]
                call(
                    manager,
                    {
                        "action": "steer",
                        "worker_id": steered_id,
                        "message": "Respond exactly: STEERED_PASS",
                    },
                )
                steered = call(
                    manager,
                    {
                        "action": "wait",
                        "worker_id": steered_id,
                        "wait_timeout_seconds": 300,
                    },
                )["results"][0]
                assert steered["status"] == "succeeded", steered
                assert "STEERED_PASS" in steered["response"], steered
                assert steered["conversation_id"], steered
                summary["steer"] = {
                    "worker_id": steered_id,
                    "conversation_id": steered["conversation_id"],
                    "response": steered["response"],
                }

                edit = call(
                    manager,
                    {
                        "action": "spawn",
                        "goal": (
                            "Create a file named worker-proof.txt in the current directory "
                            "containing exactly AGY_EDIT_OK followed by a newline. Then verify it."
                        ),
                        "task_type": "implementation",
                        "execution_mode": "accept_edits",
                        "workdir": workdir,
                        "timeout_seconds": 240,
                        "wait": True,
                        "wait_timeout_seconds": 300,
                    },
                )["results"][0]
                proof_path = Path(workdir) / "worker-proof.txt"
                assert edit["status"] == "succeeded", edit
                assert proof_path.read_text(encoding="utf-8") == "AGY_EDIT_OK\n"
                summary["accept_edits"] = {
                    "worker_id": edit["worker_id"],
                    "model": edit["model"],
                    "file_contents": proof_path.read_text(encoding="utf-8"),
                }

                cancel_spawn = call(
                    manager,
                    {
                        "action": "spawn",
                        "goal": "Use the terminal to run sleep 60, then respond exactly: TOO_LATE",
                        "task_type": "simple",
                        "workdir": workdir,
                        "timeout_seconds": 180,
                    },
                )
                cancel_id = cancel_spawn["workers"][0]["worker_id"]
                running = wait_for_status(manager, cancel_id, {"running"}, timeout=20)
                assert running["pid"], running
                call(manager, {"action": "stop", "worker_id": cancel_id})
                cancelled = wait_for_status(
                    manager,
                    cancel_id,
                    {"cancelled", "failed"},
                    timeout=20,
                )
                assert cancelled["status"] == "cancelled", cancelled
                summary["cancel"] = {
                    "worker_id": cancel_id,
                    "pid": running["pid"],
                    "status": cancelled["status"],
                }
            finally:
                manager.shutdown()

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
