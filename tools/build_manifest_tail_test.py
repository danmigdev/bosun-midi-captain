#!/usr/bin/env python3
"""Offline tests for the deterministic Captain manifest-tail builder."""

from __future__ import annotations

import builtins
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().with_name("build_manifest_tail.py")
SPEC = importlib.util.spec_from_file_location("build_manifest_tail_under_test", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def assembled(tail: bytes, request_id: str = "test-id") -> dict:
    prefix = b'{"type":"MANIFEST","id":' + json.dumps(
        request_id, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return json.loads(prefix + tail)


class ManifestTailBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bosun-manifest-tail-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def copy_sources(self) -> None:
        names = (
            MODULE.MESSAGES_SOURCE,
            MODULE.REGISTRY_SOURCE,
            *MODULE.PLUGIN_SOURCES,
        )
        for relative_name in names:
            source = MODULE.REPO_ROOT.joinpath(*relative_name.split("/"))
            destination = self.root.joinpath(*relative_name.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def test_real_tail_has_exact_wire_contract_and_registry_semantics(self):
        tail = MODULE.build_manifest_tail(MODULE.REPO_ROOT)

        self.assertTrue(tail.startswith(b',"core_messages":'))
        self.assertTrue(tail.endswith(b"}}\n"))
        self.assertEqual(tail.count(b"\n"), 1)
        tail.decode("ascii")

        wire = assembled(tail, "dynamic-request-id")
        core, plugins = MODULE.load_manifest_values(MODULE.REPO_ROOT)
        expected = {
            "type": "MANIFEST",
            "id": "dynamic-request-id",
            "core_messages": MODULE._json_semantics(core),
            "plugins": MODULE._json_semantics(plugins),
        }
        self.assertEqual(wire, expected)
        self.assertEqual(
            set(wire["plugins"]),
            {
                "kemper_player",
                "headrush_core",
                "ampero_ii_stage",
                "line6_helix",
                "generic_midi",
            },
        )
        for entry in wire["plugins"].values():
            self.assertEqual(
                set(entry),
                {
                    "label",
                    "version",
                    "messages",
                    "default_layout",
                    "tft_fields",
                    "config_schema",
                    "recipe_schema",
                },
            )

    def test_repeated_builds_are_byte_identical(self):
        first = MODULE.build_manifest_tail(MODULE.REPO_ROOT)
        second = MODULE.build_manifest_tail(MODULE.REPO_ROOT)
        self.assertEqual(first, second)

    def test_canonical_encoding_ignores_dictionary_insertion_order(self):
        first = {"outer": {"z": 1, "a": [3, 2, 1]}, "alpha": True}
        second = {"alpha": True, "outer": {"a": [3, 2, 1], "z": 1}}
        self.assertEqual(MODULE._canonical_json(first), MODULE._canonical_json(second))
        self.assertNotIn(b": ", MODULE._canonical_json(first))

    def test_semantic_validation_rejects_valid_but_changed_json(self):
        core, plugins = MODULE.load_manifest_values(MODULE.REPO_ROOT)
        tail = MODULE.build_manifest_tail(MODULE.REPO_ROOT)
        changed = tail.replace(b"Control Change", b"Control Changed", 1)
        self.assertNotEqual(tail, changed)

        with self.assertRaisesRegex(MODULE.ManifestBuildError, "semantically equal"):
            MODULE.validate_tail(changed, core, plugins)

    def test_plugins_do_not_import_hardware_modules_on_the_host(self):
        hardware_modules = {
            "adafruit_st7789",
            "analogio",
            "board",
            "busio",
            "digitalio",
            "displayio",
            "microcontroller",
            "neopixel",
            "usb_cdc",
            "usb_midi",
        }
        before = hardware_modules.intersection(sys.modules)

        MODULE.build_manifest_tail(MODULE.REPO_ROOT)

        self.assertEqual(hardware_modules.intersection(sys.modules), before)

    def test_added_plugin_import_is_blocked_before_python_can_resolve_it(self):
        bad_source = self.root / "bad_plugin.py"
        bad_source.write_text("import board\nNAME = 'bad'\n", encoding="utf-8")

        with mock.patch.object(
            builtins, "__import__", side_effect=AssertionError("real import was called")
        ):
            with self.assertRaisesRegex(MODULE.ManifestBuildError, "forbidden import 'board'"):
                MODULE._load_source_module(bad_source, "plugins.bad")

    def test_check_mode_detects_source_drift_and_never_repairs_artifact(self):
        self.copy_sources()
        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(
                MODULE.main(["--repo-root", str(self.root), "--write"]), 0
            )
        destination = self.root.joinpath(*MODULE.OUTPUT_NAME.split("/"))
        original = destination.read_bytes()

        messages = self.root.joinpath(*MODULE.MESSAGES_SOURCE.split("/"))
        with messages.open("a", encoding="utf-8") as stream:
            stream.write(
                "\nCORE_MESSAGE_TYPES['manifest_test_only'] = "
                "{'label': 'Drift', 'params': {}, 'summary': 'Drift'}\n"
            )

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = MODULE.main(["--repo-root", str(self.root), "--check"])

        self.assertEqual(result, 2)
        self.assertIn("artifact is stale", stderr.getvalue())
        self.assertEqual(destination.read_bytes(), original)

    def test_missing_artifact_check_fails_closed_without_creating_it(self):
        self.copy_sources()
        destination = self.root.joinpath(*MODULE.OUTPUT_NAME.split("/"))
        self.assertFalse(destination.exists())
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = MODULE.main(["--repo-root", str(self.root), "--check"])

        self.assertEqual(result, 2)
        self.assertIn("artifact is missing", stderr.getvalue())
        self.assertFalse(destination.exists())

    def test_write_is_atomic_and_identical_second_write_is_a_noop(self):
        self.copy_sources()
        expected = MODULE.build_manifest_tail(self.root)
        destination = MODULE.output_path(self.root)

        self.assertTrue(MODULE.write_artifact(expected, destination))
        first_stat = destination.stat()
        self.assertFalse(MODULE.write_artifact(expected, destination))
        second_stat = destination.stat()

        self.assertEqual(destination.read_bytes(), expected)
        self.assertEqual(first_stat.st_mtime_ns, second_stat.st_mtime_ns)
        self.assertEqual(list(destination.parent.glob(".manifest-tail.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
