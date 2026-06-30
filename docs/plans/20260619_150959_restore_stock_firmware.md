# Restore stock MIDI Captain firmware (undo bosun)

## Goal
Reinstall the original PaintAudio/PySwitch firmware from a backup ZIP so the
pedal is exactly as it was before bosun was ever installed, and provide a
reusable script to do it on demand.

## Source of truth
`C:\Users\danmigdev\Desktop\midi_captain_backup_20260617.zip` - a full snapshot
of the stock `CIRCUITPY` drive:
- root: `boot.py`, `code.py` (`import pyswitch.process`), `config.py`,
  `communication.py`, `display.py`, `inputs.py`, `fxcap.txt`, `boot_out.txt`
- `fonts/` (PaintAudio `.pcf`)
- `lib/` (adafruit + `pyswitch`, mostly `.mpy`)
- macOS junk: `.fseventsd/`, `.metadata_never_index`, `.Trashes`

`boot_out.txt` reports `Adafruit CircuitPython 7.3.3 ... Board ID:raspberry_pi_pico`.

## Why a file copy alone is not enough
- bosun installs **CircuitPython 9.x** (the wizard flashes 9.2.7); the backup's
  `lib/*.mpy` are compiled for the **CP7 mpy ABI** and will not import on CP9.
- bosun's own files would remain on the FAT and shadow the stock layout.

A faithful restore therefore must:
1. Enter the RP2040 ROM bootloader (`RPI-RP2`).
2. Erase the flash (`flash_nuke.uf2`) so no bosun residue and a fresh FAT.
3. Flash **CircuitPython 7.3.3** (the exact version the backup expects).
4. Copy the backup tree onto the fresh `CIRCUITPY`.

The target CP version + board are read FROM the backup's `boot_out.txt`, so the
same script restores any future backup correctly.

## Script: `tools/restore_stock.ps1`
PowerShell 5.1 compatible. Steps:
- Extract backup ZIP to a temp dir; parse `boot_out.txt` for version/board.
- Download + cache `circuitpython-<board>-<version>.uf2` and `flash_nuke.uf2`
  (`$env:LOCALAPPDATA\bosun-restore`); offline reuse if already cached.
- Enter bootloader: if `RPI-RP2` already mounted, use it; else send the REPL
  `microcontroller.on_next_reset(BOOTLOADER)` sequence over the chosen COM port
  (same trick as `tools/enter_bootloader.py`); `-ManualBootloader` to skip and
  just wait for the user to BOOTSEL.
- Copy `flash_nuke.uf2` -> wait for `RPI-RP2` to re-enumerate (unless `-SkipNuke`).
- Copy CP `.uf2` -> wait for `CIRCUITPY`.
- Copy backup files onto `CIRCUITPY` (excluding macOS junk, `__pycache__`,
  the old `boot_out.txt`).
- Flush/eject and tell the user to power-cycle.

Flags: `-BackupZip`, `-Port`, `-ManualBootloader`, `-SkipNuke`, `-AssumeYes`.

## Safety
- Destructive (erases the pedal). Confirmation prompt unless `-AssumeYes`.
- Never touches the bosun repo or the editor; only the pedal and a cache dir.
- Per existing policy: no factory-restore feature is added to the editor UI;
  this stays a standalone maintenance script.

## Status
- [x] Plan
- [x] Script written (`tools/restore_stock.ps1`)
- [x] Verified on hardware (2026-06-19): erased, flashed CP 7.3.3, restored 179
      files, auto-rebooted into stock firmware; drive hidden like a factory unit.

## Automation notes (learned on the run)
- Port auto-detect: match USB `VID_239A` (CircuitPython) / `VID_2E8A` (Pico),
  prefer interface `MI_00` (REPL console). No `-Port` needed.
- Bootloader entry MUST use `RunMode.UF2` (with getattr fallback), NOT
  `RunMode.BOOTLOADER` - the latter does not enter the ROM bootloader on
  RP2040/CP9. Broadcast to all ports + assert DTR/RTS (matches the editor's
  `serial::reboot_to_bootloader`).
- Final auto-reboot: wait ~4 s after the file copy before the REPL Ctrl-C +
  `microcontroller.reset()`, else it races CircuitPython's post-copy auto-reload.
  Retry up to 3x; CIRCUITPY vanishing = stock boot.py ran = success.

## Fully-automatic usage
    pwsh -File tools\restore_stock.ps1 -AssumeYes
