#!/usr/bin/env python3
"""Offline safety tests for remove_via_repl.py."""

import importlib.util
from pathlib import Path
import unittest


PATH = Path(__file__).with_name("remove_via_repl.py")
SPEC = importlib.util.spec_from_file_location("remove_via_repl", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ReplCleanupSafetyTests(unittest.TestCase):
    def test_accepts_only_canonical_absolute_staging_files(self):
        self.assertEqual(
            MODULE.validate_temp_path("/lib/captain/app.py.tmp"),
            "/lib/captain/app.py.tmp",
        )
        self.assertEqual(
            MODULE.validate_temp_path("/lib/plugins/kemper.mpy.recovery"),
            "/lib/plugins/kemper.mpy.recovery",
        )
        for value in (
            "relative.tmp", "/", "/lib/../code.py.tmp", "/lib//x.tmp",
            "/lib/app.py", "/lib/x.tmp;import os", "/lib/x.tmp\n/code.py",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MODULE.validate_temp_path(value)

    def test_probe_parser_requires_exact_complete_results(self):
        paths = ("/lib/a.tmp", "/lib/b.recovery")
        reply = (
            b"OK\x1b]title\x07__BOSUN_FILE__|/lib/a.tmp|192|32768\r\n"
            b"__BOSUN_MISSING__|/lib/b.recovery\r\n\x04\x04>"
        )
        self.assertEqual(
            MODULE.parse_probe_reply(reply, paths),
            {"/lib/a.tmp": (192, 32768), "/lib/b.recovery": None},
        )
        with self.assertRaises(RuntimeError):
            MODULE.parse_probe_reply(b"OK\x04\x04>", paths)
        with self.assertRaises(RuntimeError):
            MODULE.parse_probe_reply(
                b"OK__BOSUN_MISSING__|/lib/a.tmp\r\n"
                b"__BOSUN_MISSING__|/lib/a.tmp\r\n\x04\x04>",
                ("/lib/a.tmp",),
            )

    def test_removal_plan_skips_missing_and_refuses_directories(self):
        self.assertEqual(
            MODULE.removable_files({
                "/lib/a.tmp": (12, 0x8000), "/lib/b.tmp": None,
            }),
            ("/lib/a.tmp",),
        )
        with self.assertRaises(RuntimeError):
            MODULE.removable_files({"/lib/a.tmp": (0, 0x4000)})

    def test_generated_source_contains_only_literal_validated_paths(self):
        paths = ("/lib/captain/app.py.tmp",)
        probe = MODULE._probe_source(paths)
        remove = MODULE._remove_source(paths)
        self.assertIn(repr(paths), probe)
        self.assertIn(repr(paths), remove)
        self.assertIn("autoreload=False", probe)
        self.assertIn("os.remove(p)", remove)


if __name__ == "__main__":
    unittest.main(verbosity=2)
