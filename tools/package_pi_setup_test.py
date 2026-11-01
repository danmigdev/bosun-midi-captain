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

    def test_boot_logo_applier_inputs_are_packaged(self):
        """install-boot-splash.sh installs the root logo applier and its units."""
        files = set(PACKAGE.setup_files())
        installer = (TOOLS / "rpi-hub/install-boot-splash.sh").read_text(encoding="utf-8")
        read = {"tools/rpi-hub/" + name for name in re.findall(r'"\$SRC/([^"]+)"', installer)}
        self.assertIn("tools/rpi-hub/boot-splash/apply_logo.py", read)
        self.assertIn("tools/rpi-hub/systemd/bosun-boot-logo.path", read)
        self.assertFalse(read - files, f"missing from the Pi package: {sorted(read - files)}")

    def test_release_trust_file_is_packaged(self):
        """install.sh writes /etc/bosun/allowed_signers from the package."""
        self.assertIn("tools/rpi-hub/allowed_signers", set(PACKAGE.setup_files()))

    def test_install_sh_required_files_are_packaged(self):
        """install.sh refuses to run without these, also from a Desktop self-update."""
        installer = (TOOLS / "rpi-hub/install.sh").read_text(encoding="utf-8")
        block = re.search(r"for required in \\\n(.*?); do", installer, re.S)
        self.assertIsNotNone(block, "install.sh no longer lists its required files this way")
        required = set(re.findall(r"tools/rpi-hub/\S+", block[1]))
        self.assertIn("tools/rpi-hub/pi-update/install_package.py", required)
        self.assertFalse(required - set(PACKAGE.setup_files()))

    def test_entry_points_are_packaged(self):
        files = set(PACKAGE.setup_files())
        for name in ("tools/rpi-hub/quick-install.sh", "tools/rpi-hub/install.sh",
                     "tools/rpi-hub/install-boot-splash.sh", "editor/stage-kiosk.html"):
            self.assertIn(name, files)


if __name__ == "__main__":
    unittest.main()
