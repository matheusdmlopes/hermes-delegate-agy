"""Real Hermes plugin discovery/registry contract with a fake agy binary."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
HERMES_CORE = Path("/home/matheusdm/.hermes/hermes-agent")
HERMES_PYTHON = HERMES_CORE / ".venv" / "bin" / "python"


CONTRACT_SCRIPT = r"""
import fcntl
import json
import os
import sys
from pathlib import Path

core, home, workdir, empty_bundled = sys.argv[1:]
sys.path.insert(0, core)
os.environ["HERMES_HOME"] = home

import hermes_cli.plugins as plugins_mod
plugins_mod.get_bundled_plugins_dir = lambda: Path(empty_bundled)
from tools.registry import registry

manager = plugins_mod.PluginManager(scope_key=str(Path(home).resolve()))
manager.discover_and_load()
loaded = manager._plugins["delegate-agy"]
assert loaded.enabled, loaded.error
assert loaded.manifest.kind == "standalone"
assert loaded.manifest.version == "0.2.1"
assert loaded.manifest.license == "MIT"
assert loaded.manifest.homepage == "https://github.com/matheusdmlopes/hermes-delegate-agy"

entry = registry.get_entry("delegate_agy", scope=manager.scope_key)
assert entry is not None
assert entry.toolset == "delegate_agy"
assert entry.check_fn() is True

spawned = json.loads(registry.dispatch(
    "delegate_agy",
    {
        "action": "spawn",
        "goal": "Hermes contract fake run",
        "workdir": workdir,
        "wait": True,
        "wait_timeout_seconds": 10,
    },
    scope=manager.scope_key,
    session_id="contract-session",
    task_id="turn-one",
))
assert spawned["success"], spawned
worker_id = spawned["results"][0]["worker_id"]
assert spawned["results"][0]["status"] == "succeeded"

status = json.loads(registry.dispatch(
    "delegate_agy",
    {"action": "status", "worker_id": worker_id},
    scope=manager.scope_key,
    session_id="contract-session",
    task_id="turn-two",
))
assert status["success"], status

plugin_runtime = entry.handler.__self__
runtime_lock = plugin_runtime.runtime_dir / f"{plugin_runtime.runtime_id}.lock"
assert manager.unload("delegate-agy") is True
assert registry.get_entry("delegate_agy", scope=manager.scope_key) is None
after_unload = json.loads(entry.handler(
    {"action": "spawn", "goal": "must not start", "workdir": workdir},
    session_id="contract-session",
))
assert after_unload["success"] is False
assert "shutting down" in after_unload["error"]

assert not runtime_lock.exists() or not plugin_runtime._runtime_lease_status(plugin_runtime.runtime_id) == "LOCKED"
"""


class HermesContractTests(unittest.TestCase):
    def test_real_discovery_registry_context_check_and_unload(self):
        with tempfile.TemporaryDirectory(prefix="agy-hermes-home-") as home:
            with tempfile.TemporaryDirectory(prefix="agy-hermes-work-") as workdir:
                home_path = Path(home)
                plugins_dir = home_path / "plugins"
                plugins_dir.mkdir()
                (plugins_dir / "delegate-agy").symlink_to(PLUGIN_DIR, target_is_directory=True)
                (home_path / "config.yaml").write_text(
                    "plugins:\n  enabled:\n    - delegate-agy\n",
                    encoding="utf-8",
                )
                bin_dir = home_path / "bin"
                bin_dir.mkdir()
                fake_agy = bin_dir / "agy"
                fake_agy.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json\n"
                    "print(json.dumps({'status': 'SUCCESS', 'response': 'fake-contract'}))\n",
                    encoding="utf-8",
                )
                fake_agy.chmod(0o755)
                empty_bundled = home_path / "empty-bundled"
                empty_bundled.mkdir()
                env = os.environ.copy()
                env["HERMES_HOME"] = home
                env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", "")])
                result = subprocess.run(
                    [
                        str(HERMES_PYTHON),
                        "-c",
                        textwrap.dedent(CONTRACT_SCRIPT),
                        str(HERMES_CORE),
                        home,
                        workdir,
                        str(empty_bundled),
                    ],
                    cwd=workdir,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_package_import_does_not_mask_nested_import_error(self):
        package_name = "delegate_agy_import_contract"
        spec = importlib.util.spec_from_file_location(
            package_name,
            PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)
        original_import = __import__

        def fail_relative_tools(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "tools" and level == 1:
                raise ImportError("nested tools import sentinel")
            return original_import(name, globals, locals, fromlist, level)

        try:
            with patch("builtins.__import__", side_effect=fail_relative_tools):
                with self.assertRaisesRegex(ImportError, "nested tools import sentinel"):
                    module.register(MagicMock())
        finally:
            sys.modules.pop(package_name, None)


if __name__ == "__main__":
    unittest.main()
