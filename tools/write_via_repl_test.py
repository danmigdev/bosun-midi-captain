#!/usr/bin/env python3
"""Tests for the fail-closed raw-REPL recovery writer."""

import hashlib
import unittest

import write_via_repl as writer


class RemoteFile:
    def __init__(self, data):
        self.data = data
        self.offset = 0
        self.calls = []
        self.closed = False

    def execute(self, _port, code, **_kwargs):
        self.calls.append(code)
        if "os.stat(" in code and "f=open(" in code:
            self.offset = 0
            return (str(len(self.data)) + "\r\n").encode()
        if code.startswith("b=f.read("):
            read_size = int(code.split("(", 1)[1].split(")", 1)[0])
            chunk = self.data[self.offset:self.offset + read_size]
            self.offset += len(chunk)
            return chunk.hex().encode() + b"\r\n"
        if code == "f.close()\ndel f":
            self.closed = True
            return b""
        raise AssertionError("unexpected raw command: %r" % code)


class WriteViaReplIdentityTests(unittest.TestCase):
    def setUp(self):
        self.real_execute = writer.execute_raw

    def tearDown(self):
        writer.execute_raw = self.real_execute

    def test_identity_hashes_exact_bounded_readback_on_host(self):
        data = bytes(range(256)) * 3 + b"final-short"
        remote = RemoteFile(data)
        writer.execute_raw = remote.execute

        identity = writer._remote_identity(object(), "/lib/captain/protocol.mpy")

        self.assertEqual(identity, (len(data), hashlib.sha256(data).hexdigest()))
        self.assertTrue(remote.closed)
        self.assertFalse(any("hashlib" in code or "sha256" in code
                             for code in remote.calls), remote.calls)
        reads = [code for code in remote.calls if code.startswith("b=f.read(")]
        self.assertEqual(len(reads),
                         (len(data) + writer.READ_CHUNK_SIZE - 1) //
                         writer.READ_CHUNK_SIZE)
        self.assertTrue(all(int(code.split("(", 1)[1].split(")", 1)[0]) <=
                            writer.READ_CHUNK_SIZE for code in reads))
        # Large readbacks take longer than the Captain's eight-second watchdog.
        # Feed on every bounded read, just as the writer does for every chunk.
        self.assertTrue(all("microcontroller.watchdog.feed()" in code
                            for code in reads))

    def test_empty_file_has_standard_sha256_and_closes(self):
        remote = RemoteFile(b"")
        writer.execute_raw = remote.execute

        identity = writer._remote_identity(object(), "/empty")

        self.assertEqual(identity, (0, hashlib.sha256(b"").hexdigest()))
        self.assertTrue(remote.closed)

    def test_short_readback_fails_closed_and_closes(self):
        remote = RemoteFile(b"x" * 70)

        def short_execute(port, code, **kwargs):
            output = remote.execute(port, code, **kwargs)
            if code.startswith("b=f.read(") and remote.offset >= 64:
                return output[:-4]
            return output

        writer.execute_raw = short_execute

        with self.assertRaisesRegex(RuntimeError, "short readback"):
            writer._remote_identity(object(), "/truncated")
        self.assertTrue(remote.closed)

    def test_non_hex_readback_fails_closed_and_closes(self):
        remote = RemoteFile(b"payload")

        def invalid_execute(port, code, **kwargs):
            if code.startswith("b=f.read("):
                remote.calls.append(code)
                return b"not-hex\r\n"
            return remote.execute(port, code, **kwargs)

        writer.execute_raw = invalid_execute

        with self.assertRaisesRegex(RuntimeError, "invalid readback"):
            writer._remote_identity(object(), "/invalid")
        self.assertTrue(remote.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
