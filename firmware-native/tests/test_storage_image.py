"""Provision the empty factory volume with real littlefs; never replace existing output."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class StorageImage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.output = self.base / "storage.bin"

    def build(self, *arguments):
        result = subprocess.run([str(ARGS.builder), *map(str, arguments)],
                                capture_output=True, text=True, timeout=40)
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        return result

    def build_empty(self, output=None):
        return self.build("--empty", "--output", output or self.output)

    def test_empty_factory_volume_is_verified_and_deterministic(self):
        result = self.build_empty()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["empty"])
        self.assertTrue(report["verified"])
        image = self.output.read_bytes()
        self.assertEqual(len(image), 4 * 1024 * 1024)
        self.assertIn(b"littlefs", image[3670016:3678208])
        second = self.base / "second.bin"
        self.assertEqual(self.build_empty(second).returncode, 0)
        # The factory installer binds this image's hash to the firmware release.
        self.assertEqual(second.read_bytes(), image)

    def test_existing_output_and_symlink_are_never_replaced(self):
        self.output.write_bytes(b"keep original")
        self.assertNotEqual(self.build_empty().returncode, 0)
        self.assertEqual(self.output.read_bytes(), b"keep original")
        alias = self.base / "alias.bin"
        alias.symlink_to(self.output)
        self.assertNotEqual(self.build_empty(alias).returncode, 0)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.output.read_bytes(), b"keep original")

    def test_traversal_and_symlinked_output_parent_rejected(self):
        (self.base / "config").mkdir()
        alias = self.base / "output-link"
        alias.symlink_to(self.base, target_is_directory=True)
        for output in (self.base / "config/../storage.bin", alias / "storage.bin"):
            self.assertNotEqual(self.build_empty(output).returncode, 0)
        self.assertFalse(self.output.exists())

    def test_only_the_empty_mode_is_accepted(self):
        for arguments in ((), ("--empty",), ("--config-root", self.base, "--output", self.output),
                          ("--empty", "--output", self.output, "extra")):
            result = self.build(*arguments)
            self.assertEqual(result.returncode, 2, (arguments, result.stderr))
            self.assertIn("Usage", result.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--builder", type=Path, required=True)
    ARGS, remaining = parser.parse_known_args()
    unittest.main(argv=[__file__, *remaining])
