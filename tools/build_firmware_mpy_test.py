#!/usr/bin/env python3
"""Offline tests for the pinned CircuitPython .mpy build tool."""

import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name("build_firmware_mpy.py")
SPEC = importlib.util.spec_from_file_location("build_firmware_mpy_under_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeCompiler:
    def __init__(self, banner=None, magic=b"C", vary=False):
        self.banner = banner or MODULE.EXPECTED_COMPILER_BANNER
        self.magic = magic
        self.vary = vary
        self.calls = []
        self.compiles = 0

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, self.banner + "\n", "")

        self.compiles += 1
        source_name = command[-1]
        output = Path(command[command.index("-o") + 1])
        source = Path(kwargs["cwd"]) / source_name
        identity = hashlib.sha256(
            source_name.encode("utf-8") + b"\0" + source.read_bytes()
        ).digest()
        if self.vary:
            identity += bytes((self.compiles,))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self.magic + b"\x06\x00\x1f" + identity)
        return subprocess.CompletedProcess(command, 0, "", "")


class FirmwareMpyBuildTests(unittest.TestCase):
    def test_default_set_covers_every_executable_runtime_module(self):
        MODULE.validate_default_source_inventory(MODULE.DEFAULT_FIRMWARE_ROOT)
        discovered = set(MODULE.discover_runtime_sources(MODULE.DEFAULT_FIRMWARE_ROOT))

        self.assertEqual(
            discovered,
            set(MODULE.DEFAULT_SOURCES) | set(MODULE.SOURCE_ONLY_RUNTIME),
        )
        self.assertEqual(tuple(sorted(MODULE.DEFAULT_SOURCES)), MODULE.DEFAULT_SOURCES)
        self.assertEqual(len(MODULE.DEFAULT_SOURCES), 20)
        self.assertIn("lib/captain/manifest_dynamic.py", MODULE.DEFAULT_SOURCES)
        self.assertIn("lib/captain_ota.py", MODULE.DEFAULT_SOURCES)
        self.assertEqual(
            MODULE.SOURCE_ONLY_RUNTIME,
            ("lib/captain/__init__.py", "lib/plugins/__init__.py"),
        )

    def test_ci_and_release_gate_verify_checked_in_mpy_with_pinned_compiler(self):
        expected_url = (
            "https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/"
            "linux-amd64/mpy-cross-linux-amd64-9.2.7.static"
        )
        expected_hash = "3e5716e158ef977fb4f4f96e29500cdff6d85da34f507329fa7f6c2540d6faf8"
        for workflow_name in ("ci.yml", "release.yml"):
            with self.subTest(workflow=workflow_name):
                workflow = (
                    SCRIPT.parent.parent / ".github" / "workflows" / workflow_name
                ).read_text(encoding="utf-8")
                self.assertIn(expected_url, workflow)
                self.assertIn(expected_hash, workflow)
                self.assertIn(
                    "build_firmware_mpy.py --compiler /tmp/mpy-cross --check",
                    workflow,
                )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "firmware"
        self.source = self.root / "lib" / "captain" / "protocol.py"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("VALUE = 42\n", encoding="utf-8")
        self.compiler = Path(self.temp.name) / "mpy-cross"
        self.compiler.write_bytes(b"fake executable for injected runner")

    def tearDown(self):
        self.temp.cleanup()

    def _populate_default_inventory(self):
        for relative in MODULE.DEFAULT_SOURCES + MODULE.SOURCE_ONLY_RUNTIME:
            source = self.root.joinpath(*relative.split("/"))
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")

    def test_inventory_rejects_a_new_source_until_explicitly_classified(self):
        self._populate_default_inventory()
        unexpected = self.root / "lib" / "captain" / "new_runtime_module.py"
        unexpected.write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaisesRegex(MODULE.BuildError, "unclassified runtime source"):
            MODULE.validate_default_source_inventory(self.root)

    def test_inventory_rejects_a_stale_entry_whose_source_is_missing(self):
        self._populate_default_inventory()
        (self.root / "lib" / "captain" / "display.py").unlink()

        with self.assertRaisesRegex(MODULE.BuildError, "listed runtime source is missing"):
            MODULE.validate_default_source_inventory(self.root)

    def test_inventory_rejects_an_unclassified_root_captain_helper(self):
        self._populate_default_inventory()
        (self.root / "lib" / "captain_future.py").write_text("VALUE = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(
            MODULE.BuildError, "unclassified runtime source: lib/captain_future.py",
        ):
            MODULE.validate_default_source_inventory(self.root)

    def test_inventory_does_not_claim_third_party_root_libraries(self):
        self._populate_default_inventory()
        for name in ("adafruit_st7789.py", "neopixel.py"):
            (self.root / "lib" / name).write_text("VALUE = 2\n", encoding="utf-8")
        MODULE.validate_default_source_inventory(self.root)
        self.assertEqual(
            set(MODULE.discover_runtime_sources(self.root)),
            set(MODULE.DEFAULT_SOURCES) | set(MODULE.SOURCE_ONLY_RUNTIME),
        )

    def test_default_build_selects_root_ota_helper_with_a_stable_source_name(self):
        self._populate_default_inventory()
        runner = FakeCompiler()
        artifacts = MODULE.build_reproducibly(
            self.compiler, self.root, MODULE.DEFAULT_SOURCES, runner=runner,
        )
        self.assertIn("lib/captain_ota.mpy", artifacts)
        self.assertEqual(len(artifacts), 20)
        helper_calls = [command for command, _ in runner.calls
                        if command[-1] == "lib/captain_ota.py"]
        self.assertEqual(len(helper_calls), 2)
        for command in helper_calls:
            self.assertEqual(command[1:3], ["-s", "lib/captain_ota.py"])

    def test_default_cli_fails_closed_before_compiling_unknown_source(self):
        self._populate_default_inventory()
        unexpected = self.root / "lib" / "plugins" / "new_profile.py"
        unexpected.write_text("VALUE = 2\n", encoding="utf-8")
        diagnostic = StringIO()

        with mock.patch.object(MODULE, "build_reproducibly") as build:
            with redirect_stderr(diagnostic):
                result = MODULE.main([
                    "--compiler", str(self.compiler),
                    "--firmware-root", str(self.root),
                    "--check",
                ])

        self.assertEqual(result, 2)
        self.assertIn("unclassified runtime source", diagnostic.getvalue())
        build.assert_not_called()

    def test_accepts_only_the_exact_circuitpython_927_identity(self):
        runner = FakeCompiler()

        identity = MODULE.verify_compiler(self.compiler, runner=runner)

        self.assertEqual(identity, MODULE.EXPECTED_COMPILER_BANNER)
        self.assertEqual(runner.calls[0][0], [str(self.compiler.resolve()), "--version"])

    def test_cli_never_falls_back_to_mpy_cross_on_path(self):
        diagnostic = StringIO()
        with redirect_stderr(diagnostic), self.assertRaises(SystemExit):
            MODULE.parse_args(["--check"])

        self.assertIn("--compiler", diagnostic.getvalue())

    def test_rejects_pypi_micropython_even_when_it_emits_mpy_v63(self):
        runner = FakeCompiler(
            "MicroPython v1.24.1 on 2024-11-30; mpy-cross emitting mpy v6.3"
        )

        with self.assertRaisesRegex(MODULE.BuildError, "PyPI"):
            MODULE.build_reproducibly(
                self.compiler,
                self.root,
                ["lib/captain/protocol.py"],
                runner=runner,
            )

        self.assertEqual(len(runner.calls), 1, "source compiled after bad identity")

    def test_rejects_other_circuitpython_patch_versions(self):
        runner = FakeCompiler(
            "CircuitPython 9.2.8 on 2025-04-01; mpy-cross emitting mpy v6.3"
        )

        with self.assertRaisesRegex(MODULE.BuildError, "expected exactly"):
            MODULE.verify_compiler(self.compiler, runner=runner)

    def test_normalizes_source_name_and_compiles_twice(self):
        runner = FakeCompiler()

        artifacts = MODULE.build_reproducibly(
            self.compiler,
            self.root,
            [r"lib\captain\protocol.py"],
            runner=runner,
        )

        self.assertEqual(list(artifacts), ["lib/captain/protocol.mpy"])
        compile_calls = runner.calls[1:]
        self.assertEqual(len(compile_calls), 2)
        for command, options in compile_calls:
            self.assertEqual(command[1:3], ["-s", "lib/captain/protocol.py"])
            self.assertEqual(command[-1], "lib/captain/protocol.py")
            self.assertEqual(Path(options["cwd"]), self.root.resolve())
            self.assertNotIn(str(self.root.resolve()), command[-1])

    def test_rejects_microPython_magic_even_after_valid_banner(self):
        runner = FakeCompiler(magic=b"M")

        with self.assertRaisesRegex(MODULE.BuildError, "MicroPython bytecode"):
            MODULE.build_reproducibly(
                self.compiler,
                self.root,
                ["lib/captain/protocol.py"],
                runner=runner,
            )

    def test_nondeterministic_compiler_never_replaces_existing_artifact(self):
        destination = self.source.with_suffix(".mpy")
        original = b"existing deployed artifact"
        destination.write_bytes(original)
        runner = FakeCompiler(vary=True)

        with self.assertRaisesRegex(MODULE.BuildError, "non-reproducible"):
            artifacts = MODULE.build_reproducibly(
                self.compiler,
                self.root,
                ["lib/captain/protocol.py"],
                runner=runner,
            )
            MODULE.write_artifacts(artifacts, self.root)

        self.assertEqual(destination.read_bytes(), original)

    def test_check_mode_reports_difference_and_writes_nothing(self):
        destination = self.source.with_suffix(".mpy")
        original = b"old artifact"
        destination.write_bytes(original)
        generated = {"lib/captain/protocol.mpy": b"C\x06\x00\x1fnew"}

        with self.assertRaisesRegex(MODULE.BuildError, "different"):
            MODULE.check_artifacts(generated, self.root)

        self.assertEqual(destination.read_bytes(), original)

    def test_separate_output_tree_preserves_firmware_artifact(self):
        destination = self.source.with_suffix(".mpy")
        original = b"live artifact"
        destination.write_bytes(original)
        staging = Path(self.temp.name) / "staging"
        generated = {"lib/captain/protocol.mpy": b"C\x06\x00\x1fstaged"}

        MODULE.write_artifacts(generated, staging)

        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(
            (staging / "lib" / "captain" / "protocol.mpy").read_bytes(),
            generated["lib/captain/protocol.mpy"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
