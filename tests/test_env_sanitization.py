"""Regression tests for strict agy environment and wrapper resolution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools


class ClassifyAgyTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-classify-")
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def _write(self, name: str, content: bytes, mode: int = 0o755) -> Path:
        path = self.tmpdir / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def test_only_real_posix_shebang_is_script(self):
        script = self._write("script", b"#!/bin/sh\nexit 0\n")
        padded = self._write("padded", b"  #!/bin/sh\nexit 0\n")
        bom = self._write("bom", b"\xef\xbb\xbf#!/bin/sh\nexit 0\n")

        self.assertEqual(tools._classify_agy_target(str(script)), "script")
        self.assertEqual(tools._classify_agy_target(str(padded)), "native")
        self.assertEqual(tools._classify_agy_target(str(bom)), "native")

    def test_recognizes_native_magic_variants(self):
        signatures = (
            b"\x7fELF",
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
            b"\xca\xfe\xba\xbf",
            b"\xbf\xba\xfe\xca",
            b"MZ\x00\x00",
        )
        for index, signature in enumerate(signatures):
            with self.subTest(signature=signature.hex()):
                path = self._write(f"native-{index}", signature + b"\x00" * 12)
                self.assertEqual(tools._classify_agy_target(str(path)), "native")
                self.assertTrue(tools._is_known_native_magic(signature))

    def test_unknown_and_missing_targets_use_strict_fallback(self):
        unknown = self._write("unknown", b"not-an-executable-format")
        self.assertEqual(tools._classify_agy_target(str(unknown)), "native")
        self.assertEqual(tools._classify_agy_target(str(self.tmpdir / "missing")), "native")


class StrictEnvironmentTests(unittest.TestCase):
    HOSTILE_ENV = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "USER": "tester",
        "LANG": "C.UTF-8",
        "LC_TIME": "pt_BR.UTF-8",
        "LC_API_KEY": "must-not-pass",
        "HERMES_API_KEY": "hermes-leak",
        "GEMINI_API_KEY": "gemini-leak",
        "GOOGLE_APPLICATION_CREDENTIALS": "/secret/gcp.json",
        "DATABASE_URL": "postgres://user:password@db/name",
        "DOCKER_AUTH_CONFIG": "registry-secret",
        "KUBECONFIG": "/secret/kubeconfig",
        "GITHUB_PAT": "github-leak",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.socket",
        "PYTHONPATH": "/evil/import",
        "LD_PRELOAD": "/evil/lib.so",
        "MISE_EXE": "/usr/bin/mise",
        "__MISE_DIFF": "opaque-parent-environment-diff",
        "ASDF_DIR": "/home/test/.asdf",
        "RANDOM_NOISE": "harmless",
    }

    def test_all_targets_receive_same_positive_allowlist(self):
        with patch.dict(os.environ, self.HOSTILE_ENV, clear=True):
            native = tools._build_env_for_target("/usr/bin/agy", kind="native")
            script = tools._build_env_for_target("/tmp/agy", kind="script")

        self.assertEqual(native, script)
        self.assertEqual(
            native,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/test",
                "USER": "tester",
                "LANG": "C.UTF-8",
                "LC_TIME": "pt_BR.UTF-8",
            },
        )

    def test_strict_environment_removes_credentials_capabilities_and_manager_state(self):
        with patch.dict(os.environ, self.HOSTILE_ENV, clear=True):
            env = tools._build_env_for_agy("/tmp/agy")

        forbidden = set(self.HOSTILE_ENV) - {"PATH", "HOME", "USER", "LANG", "LC_TIME"}
        self.assertTrue(forbidden.isdisjoint(env), forbidden.intersection(env))
    def test_strict_environment_removes_relative_path_entries(self):
        hostile = {
            "PATH": ".:/usr/bin::relative/bin:/bin",
            "HOME": "/home/test",
        }
        with patch.dict(os.environ, hostile, clear=True):
            env = tools._build_env_allowlist()
        self.assertEqual(env["PATH"], "/usr/bin:/bin")


class WrapperResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-resolve-")
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        tools._AGY_PROBE_CACHE.clear()

    def _write(self, name: str, content: bytes) -> Path:
        path = self.tmpdir / name
        path.write_bytes(content)
        path.chmod(0o755)
        return path

    def test_mise_wrapper_resolves_real_executable_with_strict_environment(self):
        wrapper = self._write(
            "agy-wrapper",
            b"#!/bin/sh\nexec mise x antigravity-cli -- agy \"$@\"\n",
        )
        manager = self._write("mise", b"#!/bin/sh\nexit 0\n")
        real = self._write("agy-real", b"\x7fELF" + b"\x00" * 16)

        def fake_which(name, path=None):
            if name == "mise":
                return str(manager)
            return None

        completed = subprocess.CompletedProcess(
            [str(manager), "which", "agy"], 0, stdout=f"{real}\n", stderr=""
        )
        hostile = {
            "PATH": str(self.tmpdir),
            "HOME": str(self.tmpdir),
            "GEMINI_API_KEY": "must-not-pass",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "__MISE_DIFF": "opaque-secret-container",
        }
        with patch.dict(os.environ, hostile, clear=True), patch(
            "tools.shutil.which", side_effect=fake_which
        ), patch("tools.subprocess.run", return_value=completed) as run:
            resolved = tools._resolve_agy_target(str(wrapper))

        self.assertEqual(resolved, str(real.resolve()))
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(manager), "which", "agy"])
        self.assertEqual(kwargs["env"], {"PATH": str(self.tmpdir), "HOME": str(self.tmpdir)})
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("GEMINI_API_KEY", kwargs["env"])
        self.assertNotIn("SSH_AUTH_SOCK", kwargs["env"])
        self.assertNotIn("__MISE_DIFF", kwargs["env"])

    def test_known_manager_wrapper_fails_closed_when_resolution_fails(self):
        wrapper = self._write("agy-wrapper", b"#!/bin/sh\nexec mise x -- agy \"$@\"\n")
        manager = self._write("mise", b"#!/bin/sh\nexit 1\n")
        failed = subprocess.CompletedProcess(
            [str(manager), "which", "agy"], 1, stdout="", stderr="not installed"
        )
        with patch.dict(os.environ, {"PATH": str(self.tmpdir), "HOME": str(self.tmpdir)}, clear=True), patch(
            "tools.shutil.which", return_value=str(manager)
        ), patch("tools.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "could not resolve"):
                tools._resolve_agy_target(str(wrapper))

    def test_generic_script_runs_with_strict_environment_without_manager_resolution(self):
        script = self._write("agy-script", b"#!/usr/bin/env python3\nprint('1.2.0')\n")
        with patch("tools.subprocess.run") as run:
            resolved = tools._resolve_agy_target(str(script))
        self.assertEqual(resolved, str(script.resolve()))
        run.assert_not_called()

    def test_version_probe_uses_strict_environment_and_devnull(self):
        native = self._write("agy-native", b"\x7fELF" + b"\x00" * 16)
        completed = subprocess.CompletedProcess(
            [str(native), "--version"], 0, stdout="agy 1.2.0", stderr=""
        )
        hostile = {
            "PATH": "/usr/bin",
            "HOME": str(self.tmpdir),
            "OPENAI_API_KEY": "must-not-pass",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
        }
        with patch.dict(os.environ, hostile, clear=True), patch(
            "tools.subprocess.run", return_value=completed
        ) as run:
            valid, resolved, error = tools._check_agy_executable(str(native))

        self.assertTrue(valid, error)
        self.assertEqual(resolved, str(native.resolve()))
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "HOME": str(self.tmpdir)})
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("SSH_AUTH_SOCK", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
