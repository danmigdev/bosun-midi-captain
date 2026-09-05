# Reproducible Captain `.mpy` builds

Bosun precompiles every executable Python module shipped in the Captain's two
production packages and its lazy helpers at the root of `lib`. The explicit,
deterministic set contains 20 modules:

- `lib/captain/app.py`
- `lib/captain/bindings.py`
- `lib/captain/board.py`
- `lib/captain/config.py`
- `lib/captain/display.py`
- `lib/captain/expression.py`
- `lib/captain/leds.py`
- `lib/captain/manifest_dynamic.py`
- `lib/captain/messages.py`
- `lib/captain/midi.py`
- `lib/captain/navigation.py`
- `lib/captain/plugin.py`
- `lib/captain/protocol.py`
- `lib/captain/store.py`
- `lib/captain_ota.py`
- `lib/plugins/ampero.py`
- `lib/plugins/generic_midi.py`
- `lib/plugins/headrush_core.py`
- `lib/plugins/kemper.py`
- `lib/plugins/line6_helix.py`

Two package initializers deliberately remain source-only:

- `lib/captain/__init__.py` is the plaintext firmware-version source read by
  the desktop installer, version bumper, and mounted-device inspection.
- `lib/plugins/__init__.py` is a package marker containing no executable
  statements, so compiling it provides no meaningful heap saving.

The default build scans `lib/captain`, `lib/plugins`, and root-level
`lib/captain_*.py` helpers to validate this classification. Other libraries at
the root of `lib`, including Adafruit dependencies, are outside this inventory.
It fails before invoking the compiler if a new `.py` module is
unclassified, a listed module disappeared, either list is unsorted, or a
non-`__init__.py` source-only exception is introduced. Discovery never selects
build inputs: `DEFAULT_SOURCES` remains the sole ordered artifact manifest.

Use [`tools/build_firmware_mpy.py`](../tools/build_firmware_mpy.py) for these
artifacts. The script is pinned to this exact compiler identity:

```
CircuitPython 9.2.7 on 2025-04-01; mpy-cross emitting mpy v6.3
```

Do **not** run `pip install mpy-cross` or `python -m mpy_cross`. That package is
MicroPython's compiler. It may also report `mpy v6.3`, but CircuitPython can
still reject the resulting file as incompatible. The build tool requires an
explicit executable path, checks the complete CircuitPython identity, and also
checks that generated files carry CircuitPython's `C` magic rather than
MicroPython's `M` magic.

## Obtain the compiler

Build `mpy-cross` from the official `adafruit/circuitpython` tag `9.2.7`, or use
Adafruit's official Linux amd64 binary:

```
https://adafruit-circuit-python.s3.amazonaws.com/bin/mpy-cross/linux-amd64/mpy-cross-linux-amd64-9.2.7.static
```

Its SHA-256 is:

```
3e5716e158ef977fb4f4f96e29500cdff6d85da34f507329fa7f6c2540d6faf8
```

Before use, running `<compiler> --version` must print the exact identity shown
above. On Windows, pass a Windows executable built from the same official
CircuitPython tag; never substitute the similarly named PyPI package.

## Safe workflow

First perform the read-only check. It compiles every source twice in temporary
directories, verifies byte-for-byte reproducibility, then compares the result
with the existing firmware artifacts:

```powershell
python tools/build_firmware_mpy.py `
  --compiler C:\path\to\circuitpython-9.2.7\mpy-cross.exe `
  --check
```

To inspect new files without changing `firmware/`, write a staging tree:

```powershell
python tools/build_firmware_mpy.py `
  --compiler C:\path\to\circuitpython-9.2.7\mpy-cross.exe `
  --output-root .build\captain-mpy
```

Only after tests pass, explicitly replace the repository artifacts:

```powershell
python tools/build_firmware_mpy.py `
  --compiler C:\path\to\circuitpython-9.2.7\mpy-cross.exe `
  --write
```

Use `--files path/to/module.py ...` for an intentional partial build. A partial
build does not enforce the complete default inventory, which lets a maintainer
rebuild one frozen module while another is being edited. Paths are always
relative to `firmware/`. The tool normalizes embedded source names to POSIX
form, for example `-s lib/captain/protocol.py`; absolute checkout paths never
enter the bytecode. Compilation finishes and both passes compare successfully
before any destination is written. Each destination is then replaced through a
same-directory temporary file.

The build script never connects to the Captain and never deploys a file. Run
the offline guard tests with:

```
python tools/build_firmware_mpy_test.py
```

Precompiling the complete runtime avoids compiling and retaining Bosun bytecode
from source in the Captain's already constrained, fragmented heap. It also
keeps OTA practical: `app.py` alone is roughly 76 KB and needs about 790
acknowledged 96-byte chunks, while its `.mpy` is about 14 KB.
`push_firmware.py` uploads compiled files and the Captain removes each source
sibling only after validating the complete staged size.

The Android and portable package scripts automatically run
`tools/sync_firmware_resources.py` before building. The derived Tauri firmware
tree remains an exact, reviewable mirror and therefore contains both forms;
the install/deploy enumerators select `.mpy` and omit its `.py` sibling. This
keeps host-side source tests available without sending the large source module
to the Captain.
