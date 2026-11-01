#!/usr/bin/env python3
"""Signing the Pi package with a throwaway key; never a committed private key."""
import gzip
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("sign_pi_package", Path(__file__).with_name("sign-pi-package.py"))
SIGN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SIGN)
KEYGEN = os.environ.get("BOSUN_SSH_KEYGEN") or shutil.which("ssh-keygen")


def supports_sshsig():
    if not KEYGEN:
        return False
    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / "probe"
        made = subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], capture_output=True)
        if made.returncode:
            return False
        return subprocess.run([KEYGEN, "-Y", "sign", "-f", str(key), "-n", "probe"],
                              input=b"probe", capture_output=True).returncode == 0


@unittest.skipUnless(supports_sshsig(), "ssh-keygen with -Y sign (OpenSSH 8.2+) is unavailable")
class SignPiPackageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bosun-pi-sign-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.make_key("release")
        self.package = self.root / "bosun-pi-setup.tar.gz"
        self.package.write_bytes(gzip.compress(b"package bytes \x00\x01\r\n" * 100, mtime=0))
        self.signature = self.root / "bosun-pi-setup.tar.gz.sig"
        self.allowed = self.root / "allowed_signers"
        self.trust(self.key)

    def make_key(self, name):
        key = self.root / name
        subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "bosun-release", "-f", str(key)], check=True)
        return key

    def trust(self, key, namespace="bosun-pi-package", identity="bosun-release"):
        public = " ".join(key.with_name(key.name + ".pub").read_text().split()[:2])
        self.allowed.write_text(f'{identity} namespaces="{namespace}" {public}\n')

    def cli(self, *args, key=None):
        env = {k: v for k, v in os.environ.items() if k != "BOSUN_PI_SIGNING_KEY"}
        if key is not None:
            env["BOSUN_PI_SIGNING_KEY"] = str(key)
        return subprocess.run([sys.executable, str(Path(__file__).with_name("sign-pi-package.py")),
                               "--package", str(self.package), "--allowed-signers", str(self.allowed), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_signs_and_verifies_against_the_release_signer(self):
        result = self.cli(key=self.key)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.signature.read_bytes()
        self.assertTrue(text.startswith(b"-----BEGIN SSH SIGNATURE-----\n"))
        self.assertNotIn(b"\r", text)
        self.assertLessEqual(len(text), 4096)
        # The exact check the Pi root installer runs.
        verified = subprocess.run([KEYGEN, "-Y", "verify", "-f", str(self.allowed), "-I", "bosun-release",
                                   "-n", "bosun-pi-package", "-s", str(self.signature)],
                                  input=self.package.read_bytes(), capture_output=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(self.cli("--check").returncode, 0)

    def test_replaces_a_stale_signature_instead_of_keeping_it(self):
        self.signature.write_text("stale")
        self.assertEqual(self.cli(key=self.key).returncode, 0)
        self.assertEqual(self.cli("--check").returncode, 0)

    def test_missing_key_is_a_clear_error_and_writes_nothing(self):
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOSUN_PI_SIGNING_KEY", result.stderr)
        result = self.cli(key=self.root / "absent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr)
        self.assertFalse(self.signature.exists())

    def test_key_outside_allowed_signers_is_refused_and_removed(self):
        other = self.make_key("other")
        result = self.cli(key=other)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.signature.exists())

    def test_wrong_namespace_or_identity_in_the_trust_file_fails(self):
        for options in ({"namespace": "other"}, {"identity": "someone"}):
            with self.subTest(**options):
                self.trust(self.key, **options)
                self.assertNotEqual(self.cli(key=self.key).returncode, 0)
                self.assertFalse(self.signature.exists())

    def test_check_refuses_a_changed_package(self):
        self.assertEqual(self.cli(key=self.key).returncode, 0)
        self.package.write_bytes(gzip.compress(b"tampered", mtime=0))
        result = self.cli("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stderr)

    def test_check_requires_a_signature(self):
        result = self.cli("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing signature", result.stderr)

    def test_without_a_release_key_only_require_mode_fails(self):
        self.allowed.write_text("# release key added later\n")
        self.assertEqual(self.cli(key=self.key).returncode, 0)
        self.assertEqual(self.cli("--check").returncode, 0)
        self.assertNotEqual(self.cli("--check", "--require-allowed-signers").returncode, 0)

    def test_refuses_non_gzip_or_oversized_packages(self):
        self.package.write_bytes(b"not gzip")
        self.assertNotEqual(self.cli(key=self.key).returncode, 0)
        self.package.write_bytes(b"\x1f\x8b" + b"\0" * (4 * 1024 * 1024))
        self.assertNotEqual(self.cli(key=self.key).returncode, 0)
        self.assertFalse(self.signature.exists())


class TrustFileTest(unittest.TestCase):
    def test_comment_only_trust_file_has_no_signers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allowed_signers"
            self.assertFalse(SIGN.has_signers(path))
            path.write_text("# none yet\n\n")
            self.assertFalse(SIGN.has_signers(path))
            path.write_text('bosun-release namespaces="bosun-pi-package" ssh-ed25519 AAAA\n')
            self.assertTrue(SIGN.has_signers(path))

    def test_repository_trust_file_names_the_release_identity(self):
        path = Path(__file__).resolve().parent / "rpi-hub/allowed_signers"
        if not path.is_file():
            self.skipTest("no release key committed yet")
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r'^bosun-release namespaces="bosun-pi-package" ssh-ed25519 \S+')


if __name__ == "__main__":
    unittest.main(verbosity=2)
