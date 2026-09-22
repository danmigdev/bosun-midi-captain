"""Host-only checks: SRAM boundaries, release binding and Pi package contents."""
import importlib.util
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("factory", ROOT / "tools/package-factory-installer.py")
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)


def loader():
    data = bytearray(1024)
    for i in range(2):
        struct.pack_into('<8I',data,i*512,0x0a324655,0x9e5d5157,0x2000,0x20000000+i*256,256,i,2,0xe48bff56)
        struct.pack_into('<I',data,i*512+508,0x0ab16f30)
    struct.pack_into('<II',data,544,0x20042000,0x200001c1)
    return data


class FactoryPackageTests(unittest.TestCase):
    def test_sram_only_and_vector_bounds(self):
        factory.validate_loader(loader())
        for offset,value in ((12,0x10000000),(524,0x20042000),(8,0),(20,1),(24,3),(28,0),(544,0x20000000),(548,0x10000101)):
            data=loader()
            struct.pack_into('<I',data,offset,value)
            with self.assertRaises(ValueError): factory.validate_loader(data)
        for size in (0,512,1023):
            with self.assertRaises(ValueError): factory.validate_loader(loader()[:size])

    def test_asset_checksums_and_firmware_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            data=loader(); storage=bytearray(4194304); storage[3670016:3670024]=b'littlefs'
            (root/'loader.uf2').write_bytes(data); (root/'storage.bin').write_bytes(storage)
            manifest={'schema':1,'firmware_sha256':'native-release',
                      'loader_sha256':factory.hashlib.sha256(data).hexdigest(),
                      'storage_sha256':factory.hashlib.sha256(storage).hexdigest()}
            (root/'manifest.json').write_text(json.dumps(manifest))
            with zipfile.ZipFile(root/'firmware.zip','w') as z:
                z.writestr('manifest.json',json.dumps({'firmware_sha256':'native-release'}))
            factory.verify(root,root/'firmware.zip')
            manifest['firmware_sha256']='different-release'
            (root/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): factory.verify(root,root/'firmware.zip')
            manifest['firmware_sha256']='native-release'
            (root/'manifest.json').write_text(json.dumps(manifest))
            storage[-1]^=1; (root/'storage.bin').write_bytes(storage)
            with self.assertRaises(ValueError): factory.verify(root,root/'firmware.zip')

    def test_built_pi_package_has_stage_sources_and_no_machine_credentials(self):
        archive=ROOT/'editor/src-tauri/resources/pi/bosun-pi-setup.tar.gz'
        if not archive.exists(): self.skipTest('Pi assets not built in source-only checkout')
        with tarfile.open(archive) as tar:
            names=tar.getnames()
            for name in ('editor/dist-stage/index.html','tools/rpi-hub/quick-install.sh',
                         'firmware-native/platform/host/storage_image.c','editor/src-tauri/icons/icon.png'):
                self.assertIn(name,names)
            self.assertTrue(all(m.isfile() and not m.name.startswith('/') and '..' not in Path(m.name).parts for m in tar.getmembers()))
            self.assertFalse(any('hostapd.conf' in n or '.env' in n or '__pycache__' in n or '.secrets' in n for n in names))
            self.assertNotIn(b'\r',tar.extractfile('tools/rpi-hub/quick-install.sh').read())


if __name__ == '__main__': unittest.main()
