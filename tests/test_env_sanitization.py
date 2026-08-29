"""Tests for environment sanitization strategy in delegate-agy.

The plugin must choose between two strategies for the subprocess env:

  - "native"   -> the agy binary is an ELF/Mach-O executable. We can safely
                  use a strict ALLOWLIST (Hermes secrets never reach the
                  worker).
  - "script"   -> the agy binary is a wrapper script (mise, asdf, rtx, nix,
                  brew). The interpreter needs MISE_*/ASDF_*/etc to locate
                  the real tool, so we use a strict DENYLIST instead and
                  inherit the parent env, then strip known secret patterns.

The classification is the heart of the fix: it lets the plugin keep its
zero-leak guarantee on native installs while remaining functional on shim
installs.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools
from tools import AgyManager


class ClassifyAgyTargetTests(unittest.TestCase):
    """The classifier must return 'script' for any file whose first bytes
    start with a #! shebang, and 'native' for any other executable file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-classify-")
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def _write(self, name: str, content: bytes, mode: int = 0o755) -> Path:
        path = self.tmpdir / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def test_classifies_bash_shim_as_script(self):
        path = self._write("agy", b"#!/bin/bash\nexec real-agy \"$@\"\n")
        self.assertEqual(tools._classify_agy_target(str(path)), "script")

    def test_classifies_env_shim_as_script(self):
        path = self._write("agy", b"#!/usr/bin/env bash\necho ok\n")
        self.assertEqual(tools._classify_agy_target(str(path)), "script")

    def test_classifies_elf_magic_as_native(self):
        # ELF magic: 0x7F 'E' 'L' 'F'
        path = self._write("agy", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8)
        self.assertEqual(tools._classify_agy_target(str(path)), "native")

    def test_classifies_mach_o_magic_as_native(self):
        # Mach-O 64-bit little-endian magic: 0xFEEDFACF
        path = self._write("agy", b"\xcf\xfa\xed\xfe" + b"\x00" * 12)
        self.assertEqual(tools._classify_agy_target(str(path)), "native")

    def test_classifies_shebang_with_whitespace_as_script(self):
        # Some shims have a leading whitespace before the shebang (rare but real).
        path = self._write("agy", b"  #!/bin/sh\necho hi\n")
        self.assertEqual(tools._classify_agy_target(str(path)), "script")

    def test_classifies_non_executable_file_as_native_safe_fallback(self):
        # If we cannot read the file or it is not executable, we must NOT
        # silently fall back to inheriting the full env. Default to "native"
        # so the allowlist path is used, which is the safer choice.
        path = self.tmpdir / "missing"
        result = tools._classify_agy_target(str(path))
        self.assertEqual(result, "native")


class BuildEnvForTargetTests(unittest.TestCase):
    """_build_env_for_target must select allowlist vs denylist per
    classification, while still dropping every Hermes secret."""

    HOSTILE_ENV = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "USER": "tester",
        "HERMES_API_KEY": "sk-hermes-leak",
        "HERMES_SESSION_TOKEN": "tok-leak",
        "OPENAI_API_KEY": "sk-openai-leak",
        "ANTHROPIC_API_KEY": "sk-anthropic-leak",
        "AWS_SECRET_ACCESS_KEY": "aws-leak",
        "GITHUB_TOKEN": "gh-leak",
        "PYTHONPATH": "/evil/import",
        "LD_PRELOAD": "/evil/lib.so",
        "MISE_EXE": "/usr/bin/mise",
        "MISE_SHELL": "bash",
        "MISE_TRUSTED_CONFIG": "1",
        "ASDF_DIR": "/home/test/.asdf",
        "RANDOM_NOISE": "harmless",
    }

    def test_native_target_uses_allowlist_and_strips_secrets(self):
        with patch.dict(os.environ, self.HOSTILE_ENV, clear=True):
            env = tools._build_env_for_target("/usr/bin/agy", kind="native")

        # Allowlist basics
        self.assertIn("PATH", env)
        self.assertIn("HOME", env)
        self.assertIn("USER", env)

        # No Hermes or third-party secrets
        for forbidden in (
            "HERMES_API_KEY",
            "HERMES_SESSION_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "PYTHONPATH",
            "LD_PRELOAD",
        ):
            self.assertNotIn(forbidden, env)

    def test_script_target_inherits_mise_and_other_loader_vars(self):
        """The whole point of the fix: a shim wrapper must see MISE_*
        and other loader vars so it can locate the real agy binary."""
        with patch.dict(os.environ, self.HOSTILE_ENV, clear=True):
            env = tools._build_env_for_target(
                "/home/test/.local/bin/agy", kind="script"
            )

        # Loader vars MUST survive
        self.assertEqual(env.get("MISE_EXE"), "/usr/bin/mise")
        self.assertEqual(env.get("MISE_SHELL"), "bash")
        self.assertEqual(env.get("ASDF_DIR"), "/home/test/.asdf")

        # And PATH/HOME/USER too (needed to even find the interpreter)
        self.assertEqual(env.get("PATH"), "/usr/bin:/bin")
        self.assertEqual(env.get("HOME"), "/home/test")

        # But secrets MUST still be stripped (denylist works)
        for forbidden in (
            "HERMES_API_KEY",
            "HERMES_SESSION_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
        ):
            self.assertNotIn(forbidden, env)

    def test_script_target_strips_loader_injection_attacks(self):
        """LD_PRELOAD / LD_LIBRARY_PATH / PYTHONPATH / DYLD_* are
        classic loader hijack vectors. Denylist must catch them even
        in script mode."""
        with patch.dict(os.environ, self.HOSTILE_ENV, clear=True):
            env = tools._build_env_for_target(
                "/home/test/.local/bin/agy", kind="script"
            )
        for forbidden in ("LD_PRELOAD", "PYTHONPATH"):
            self.assertNotIn(forbidden, env)


class DispatchTests(unittest.TestCase):
    """The public entry point must detect the kind and dispatch to the
    matching builder. It is the only function _execute_once will call."""

    def test_dispatches_to_native_builder_for_native_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            elf = Path(tmp) / "agy"
            elf.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 8)
            elf.chmod(0o755)
            with patch.dict(os.environ, {"MISE_EXE": "/usr/bin/mise"}, clear=True):
                env = tools._build_env_for_agy(str(elf))
            self.assertNotIn("MISE_EXE", env)

    def test_dispatches_to_script_builder_for_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "agy"
            shim.write_bytes(b"#!/bin/bash\necho shim\n")
            shim.chmod(0o755)
            with patch.dict(
                os.environ,
                {"MISE_EXE": "/usr/bin/mise", "PATH": "/bin", "HOME": "/tmp"},
                clear=True,
            ):
                env = tools._build_env_for_agy(str(shim))
            self.assertEqual(env.get("MISE_EXE"), "/usr/bin/mise")


if __name__ == "__main__":
    unittest.main()
