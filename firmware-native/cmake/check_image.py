#!/usr/bin/env python3
"""Inspect built ELF/UF2 files only; never probes or opens a hardware device."""
import argparse
import json
from pathlib import Path
import struct
import subprocess


def audit(elf: Path, uf2: Path, flash_bytes: int, nm: str) -> dict:
    symbols = {}
    for line in subprocess.check_output([nm, '--defined-only', str(elf)], text=True).splitlines():
        fields = line.split()
        if len(fields) == 3:
            symbols[fields[2]] = int(fields[0], 16)
    required = ('__flash_binary_start', '__flash_binary_end', '__bss_start__',
                '__bss_end__', '__end__', '__HeapLimit', '__StackTop', '__StackBottom')
    for name in required:
        if name not in symbols:
            raise ValueError(f'missing ELF bound: {name}')
    flash_limit = 0x10000000 + flash_bytes - 512 * 1024
    if not 0x10000000 <= symbols['__flash_binary_start'] < symbols['__flash_binary_end'] <= flash_limit:
        raise ValueError('ELF flash image overlaps reserved storage or lies outside XIP flash')
    stack = symbols['__StackTop'] - symbols['__StackBottom']
    margin = symbols['__StackBottom'] - symbols['__end__']
    if stack != 16384 or margin < 16384 or symbols['__HeapLimit'] != symbols['__end__']:
        raise ValueError('ELF violates the fixed stack, RAM margin, or zero-heap contract')
    allocators = sorted(set(symbols) & {'malloc', 'calloc', 'realloc', '_malloc_r',
                                      '_calloc_r', '_realloc_r', '__wrap_malloc',
                                      '__wrap_calloc', '__wrap_realloc'})
    if allocators:
        raise ValueError(f'dynamic allocation linked into the hardware image: {allocators}')
    binary = uf2.read_bytes()
    if not binary or len(binary) % 512:
        raise ValueError('UF2 is empty or not aligned to 512-byte blocks')
    count = len(binary) // 512
    addresses = set()
    for index in range(count):
        block = binary[index * 512:(index + 1) * 512]
        magic0, magic1, flags, address, length, number, total, family = struct.unpack_from('<8I', block)
        if (magic0, magic1, struct.unpack_from('<I', block, 508)[0]) != (0x0a324655, 0x9e5d5157, 0x0ab16f30):
            raise ValueError(f'invalid UF2 magic in block {index}')
        if flags != 0x2000 or family != 0xe48bff56 or number != index or total != count:
            raise ValueError(f'invalid RP2040 UF2 metadata in block {index}')
        if length != 256 or address % 256 or address in addresses:
            raise ValueError(f'invalid or duplicate UF2 page in block {index}')
        if not 0x10000000 <= address or address + length > flash_limit:
            raise ValueError(f'UF2 block {index} overlaps reserved storage or lies outside flash')
        addresses.add(address)
    return {'elf': str(elf), 'uf2': str(uf2), 'flash_image_bytes':
            symbols['__flash_binary_end'] - symbols['__flash_binary_start'],
            'static_ram_bytes': symbols['__end__'] - 0x20000000,
            'bss_bytes': symbols['__bss_end__'] - symbols['__bss_start__'],
            'stack_bytes': stack, 'unused_ram_margin_bytes': margin,
            'storage_offset': flash_bytes - 512 * 1024, 'storage_bytes': 512 * 1024,
            'uf2_blocks': count, 'dynamic_allocator_symbols': allocators}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--uf2', type=Path, required=True)
    parser.add_argument('--flash-bytes', type=int, required=True)
    parser.add_argument('--nm', default='arm-none-eabi-nm')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    report = audit(args.elf, args.uf2, args.flash_bytes, args.nm)
    rendered = json.dumps(report, indent=2) + '\n'
    if args.report:
        args.report.write_text(rendered, encoding='utf-8')
    print(rendered, end='')
