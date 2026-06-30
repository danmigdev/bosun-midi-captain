# Auto-detect an unflashed pedal and offer to install (with backup warning)

## Problem / request
On the welcome screen the editor silently `autoConnect()`s and only attaches if
a port ACKs the bosun protocol. A factory/stock pedal never ACKs and HIDES its
CIRCUITPY drive, so the editor just sits at "Disconnected" with no hint that a
flashable pedal is plugged in. The user must know to click "Install firmware ->".

Desired UX (user): when a connected pedal has no bosun firmware, the editor
should notice and proactively start the install procedure, asking the user to
confirm first - and the confirmation must warn that installing ERASES whatever
is on the pedal, so the user should back it up first.

Chosen prompt style: auto-open the install wizard with a confirmation step
("A pedal without Bosun firmware is connected. Install now?"). On "Not now" it
closes and does not nag again until the device is unplugged/replugged.

## Why the old drive-probe couldn't do this
The previously-reverted probe looked only at drives (RPI-RP2/CIRCUITPY). A stock
pedal exposes neither (boot.py hides the drive), so drives alone can't see it.
The reliable signal is the USB VID of the serial device: a CircuitPython unit is
`VID 239A` (Adafruit) and the RP2 ROM bootloader is `VID 2E8A` (Raspberry Pi).
That is exactly what `tools/restore_stock.ps1` uses to find the pedal.

## Detection limits (=> confirmation-gated, never silent install)
- VID 239A/PID 80F4 is the generic raspberry_pi_pico CircuitPython id - shared by
  bosun, the stock firmware, and any bare Pico. We cannot tell a MIDI Captain
  from a plain Pico by USB id, so the confirm step is the safety net.
- An already-flashed bosun whose data CDC is temporarily down (the "needs hard
  reset" state) could also trip it; reinstalling fixes that, and confirm covers it.

## Implementation

### Rust (`editor/src-tauri`)
- Add `serialport = "4"` (enumeration ONLY; serial2 stays the live-handle layer).
- `installer.rs`: new `DeviceState.usb_pedal_present: bool`, set by scanning
  `serialport::available_ports()` for a `UsbPort` with vid `0x239A` or `0x2E8A`.

### Frontend (`editor/src`)
- `installer.ts`: add `usb_pedal_present: boolean` to `DeviceState`.
- `App.svelte`: while disconnected and the installer is closed, poll
  `detectPedal()`. If `!connected && usb_pedal_present && !has_captain_firmware`
  and not already dismissed -> auto-open the Installer in confirm mode. Reset the
  "dismissed" latch when the pedal disappears so a replug re-prompts.
- `Installer.svelte`: a `requireConfirm` prop. When set, show a confirmation gate
  BEFORE any action: explains the install ERASES the pedal and to back up first;
  [Not now] closes, [Install] runs the existing `doAutoSetup()`. Also surface the
  same back-up warning in the manual destructive phases.

### Policy
- Warn-only. No in-editor backup/restore button (matches existing decision); the
  user backs up themselves (e.g. via the stock backup ZIP).

## Verify
- `cargo check` (src-tauri) compiles with the new dep + field.
- `npm run check` (svelte-check) passes with the new prop/field.
- Manual: stock pedal connected -> editor auto-opens the confirm dialog.

## Status
- [x] Rust dep (`serialport 4`) + `usb_pedal_present` field in detect_pedal
- [x] TS field + App.svelte poll/auto-open + Installer confirm gate + backup warning
- [x] Build/typecheck: `npm run check` clean; `cargo check` OK (serialport 4.9.0)
- [ ] Manual test on the stock pedal (needs a portable rebuild + the pedal)

## Follow-up: bootloader entry actually failed on the stock pedal (fixed)
First on-hardware test: the wizard auto-opened correctly (detection works), but
"Set up pedal automatically" failed with "could not write to any serial port".
Root cause (diagnosed live, Bosun closed, isolated): the running factory
PaintAudio firmware does NOT drain its USB CDC console, so EVERY host write to
COM3 times out ("semaphore timeout") - DTR or not, editor or not. The editor's
`reboot_to_bootloader` only drove the REPL via writes, so it could never reach
the bootloader on this firmware.

Fix: the **1200-baud touch** (open the CDC at 1200 baud, drop DTR, close) makes
CircuitPython reset into RPI-RP2 with no write required - verified live, RPI-RP2
appeared immediately. `serial::reboot_to_bootloader` now does the 1200-baud
touch FIRST on every port (counts as success even though no bytes are sent),
then the REPL Ctrl-C write as a fallback. Works on CP 7.3.3 and 9.x alike.
See [[project_bootloader_entry_1200_touch]].

## Follow-up 2: auto-prompt missed the bootloader state (fixed)
First broadened trigger only keyed on `usb_pedal_present` (serial VID). A pedal
sitting in the RPI-RP2 bootloader is MASS STORAGE with no serial port, so it
never prompted. `pollForUnflashedPedal` now prompts for any install-needing
state: in bootloader, CIRCUITPY without captain firmware, wrong CircuitPython,
or stock-serial. Only the stock-serial path tries `autoConnect()` first (to let
a healthy bosun attach silently); drive/bootloader states prompt directly.

## Follow-up 3: wizard stuck after a successful install (fixed)
Install worked (38 files copied, TFT showed "(unnamed)", both bosun CDC ports
came up) but the wizard kept showing "Set up pedal automatically". Cause: a
running bosun HIDES its CIRCUITPY drive, so the drive-based detect_pedal reports
no_device and the wizard can't see success. Fix: Installer takes an
`onInstalled` callback; after `installFirmware` it calls it and the App closes
the wizard, runs `tryReconnect()` against the rebooted pedal (data port comes up
after the self-heal reset), and refetches. So a finished install now lands the
user in the connected editor instead of a dead-end wizard.

## Follow-up 4: "firmware: not_found" toast after a fresh install (fixed)
Captured via temp-log instrumentation (`debug_log` command + `dbg()` in App):
```
handleInstalled: tryReconnect -> true
TOAST[ok] Firmware installed. Pedal connected.
MANIFEST gave up after 5 retries (fw=0.3.23, profile=)   <- no profile
TOAST[error] firmware: not_found
```
Cause: a freshly-installed pedal has NO active profile. The editor's refetchAll
fetched manifest/patches/global anyway; the firmware answers `not_found`, which
renders via flashFirmwareError as a `firmware: not_found` toast, plus the
manifest watchdog "gave up" and showed the misleading "re-flash firmware" panel.
A true first-timer would have been routed through Onboarding, but a returning
user (BOSUN_ONBOARDED=1) skipped it and hit the raw errors.

Fix (App.svelte):
- refetchAll resolves the active profile first (listProfiles) and SKIPS
  manifest/patch/global queries when there's none.
- manifest watchdog gated on `hasActiveProfile` (no profile -> no retries / no
  "gave up").
- ERROR handler suppresses `not_found` when there's no profile.
- main view shows a "Create your first profile" CTA instead of the manifest
  spinner/error when profile-less.
- an effect opens Onboarding once for a connected profile-less pedal, regardless
  of the BOSUN_ONBOARDED flag, then re-arms after a profile exists.

NOTE: temporary `installer::debug_log` + `dbg()` logging to %TEMP%\bosun-debug.log
is still in the tree for diagnosis - STRIP before the version bump / ship.

## Follow-up 5: import config with no profile created an "undefined" profile (fixed)
With no profiles, the import dialog's "Overwrite active profile" path called
`importConfig` with no target profile, so PUT_* wrote to a non-existent active
profile -> a bogus "undefined" profile. Fix (MaintenancePanel.svelte): when
there is no `activeProfile`, the "Overwrite" option is disabled (with a hint),
the import is forced down the create-new path, the "Create & restore" button is
disabled until a profile name is entered, and `confirmImport` defensively forces
`importMode = "new"`. Batch (multi-file) import already always creates new
profiles, so it was unaffected.

## Implementation notes
- serial2 (connection layer) exposes no USB metadata, so enumeration uses the
  `serialport` crate (added default-features=false). serial2 unchanged.
- Poll only runs while disconnected + installer closed + not manual mode; it
  attempts a real `autoConnect()` first, so a healthy-but-unattached bosun
  attaches silently and never triggers the install prompt (only a non-ACKing
  stock pedal does). "Not now" latches until the pedal is unplugged.
