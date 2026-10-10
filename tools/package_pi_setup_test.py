#!/usr/bin/env python3
"""The exported Pi package must carry every repository file its installers read."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
import unittest


TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("package_pi_setup_under_test", TOOLS / "package-pi-setup.py")
PACKAGE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = PACKAGE
SPEC.loader.exec_module(PACKAGE)


class PiSetupPackageTest(unittest.TestCase):
    def test_boot_splash_inputs_are_packaged(self):
        """An update with the boot splash enabled reruns install-boot-splash.sh,
        which reads these through boot-splash/configure.py."""
        files = set(PACKAGE.setup_files())
        configure = (TOOLS / "rpi-hub/boot-splash/configure.py").read_text(encoding="utf-8")
        read = set(re.findall(r'repo / "([^"]+)"', configure))
        self.assertTrue(read, "configure.py no longer reads repository files this way")
        self.assertFalse(read - files, f"missing from the Pi package: {sorted(read - files)}")

    def test_entry_points_are_packaged(self):
        files = set(PACKAGE.setup_files())
        for name in ("tools/rpi-hub/quick-install.sh", "tools/rpi-hub/install.sh",
                     "tools/rpi-hub/install-boot-splash.sh", "editor/stage-kiosk.html"):
            self.assertIn(name, files)


if __name__ == "__main__":
    unittest.main()
