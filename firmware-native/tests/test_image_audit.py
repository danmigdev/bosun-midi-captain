"""Reject unsafe output artifacts independently of the linker that made them."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('check_image', Path(__file__).resolve().parents[1] / 'cmake/check_image.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ImageAudit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.uf2 = Path(self.temporary.name) / 'image.uf2'
        self.symbols = {'__flash_binary_start': 0x10000000, '__flash_binary_end': 0x10000100,
                        '__bss_start__': 0x20001000, '__bss_end__': 0x20020000,
                        '__end__': 0x20020000, '__HeapLimit': 0x20020000,
                        '__StackBottom': 0x2003e000, '__StackTop': 0x20042000}
        self.block = bytearray(512)
        struct.pack_into('<8I', self.block, 0, 0x0a324655, 0x9e5d5157, 0x2000,
                         0x10000000, 256, 0, 1, 0xe48bff56)
        struct.pack_into('<I', self.block, 508, 0x0ab16f30)

    def audit(self, flash_bytes=8 * 1024 * 1024):
        self.uf2.write_bytes(self.block)
        listing = ''.join(f'{value:08x} T {name}\n' for name, value in self.symbols.items())
        with patch.object(module.subprocess, 'check_output', return_value=listing):
            return module.audit(Path('image.elf'), self.uf2, flash_bytes, 'fake-nm')

    def test_valid_image(self):
        report = self.audit()
        self.assertEqual(report['stack_bytes'], 16384)
        self.assertEqual(report['flash_image_bytes'], 256)
        self.assertEqual(report['dynamic_allocator_symbols'], [])
        self.assertEqual(report['storage_offset'], 0x780000)
        self.assertEqual(report['storage_bytes'], 512 * 1024)

    def test_storage_overlap(self):
        struct.pack_into('<I', self.block, 12, 0x10780000)
        with self.assertRaisesRegex(ValueError, 'reserved storage'):
            self.audit()

    def test_forged_family(self):
        struct.pack_into('<I', self.block, 28, 0xe48bff59)
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.audit()

    def test_linked_allocator(self):
        self.symbols['__wrap_malloc'] = 0x100000a0
        with self.assertRaisesRegex(ValueError, 'dynamic allocation'):
            self.audit()

    def test_inadequate_ram_margin(self):
        self.symbols['__end__'] = self.symbols['__HeapLimit'] = 0x2003c000
        with self.assertRaisesRegex(ValueError, 'RAM margin'):
            self.audit()

    def test_elf_storage_overlap(self):
        self.symbols['__flash_binary_end'] = 0x10780001
        with self.assertRaisesRegex(ValueError, 'ELF flash image'):
            self.audit()

    def test_truncated_uf2(self):
        self.block.pop()
        with self.assertRaisesRegex(ValueError, '512-byte'):
            self.audit()

    def test_explicit_smaller_flash_geometry_still_checked(self):
        self.assertEqual(self.audit(2 * 1024 * 1024)['storage_offset'], 0x180000)
        struct.pack_into('<I', self.block, 12, 0x10180000)
        with self.assertRaisesRegex(ValueError, 'reserved storage'):
            self.audit(2 * 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
