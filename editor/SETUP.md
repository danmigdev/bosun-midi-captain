# Bosun editor setup

One-time install steps, then the dev loop.

## Prerequisites

| Tool | Status on this machine | How to install |
|---|---|---|
| Node + npm | already present (Node 20+) | https://nodejs.org/ |
| Rust + Cargo | **missing** | Run `rustup-init.exe` from https://rustup.rs |
| WebView2 | bundled with Windows 11 | nothing to do |
| Microsoft C++ Build Tools | usually present | Visual Studio Build Tools 2022 if `cargo build` complains about a linker |

## Install Rust (the missing one)

1. Download `rustup-init.exe` from https://rustup.rs
2. Run it, accept the defaults (`1` then Enter). Picks the MSVC toolchain.
3. Restart any open terminal so `cargo` lands on `PATH`.
4. Verify: `cargo --version` and `rustc --version` both print a version.

## Install JS deps

From `editor/`:

```
npm install
```

This pulls in `@tauri-apps/api`, `@tauri-apps/cli`, Svelte, Vite, TypeScript.

## Download installer assets

The **Pedal setup** wizard needs the CircuitPython UF2 image and a few
Adafruit libraries bundled with the editor. One-time fetch (and re-run
whenever you bump CircuitPython or the firmware tree):

```
pwsh -File tools\download-assets.ps1
```

This populates `editor/src-tauri/resources/` with:

- `circuitpython.uf2` - RP2040 build of CircuitPython
- `lib/adafruit_display_text/`, `lib/adafruit_st7789.mpy`, `lib/neopixel.mpy`, `lib/adafruit_pixelbuf.mpy`
- `firmware/` - mirror of the repo's top-level `firmware/` tree

If you skip this step the editor still launches, but the Pedal Setup wizard
will report "Installer assets are missing" and refuse to flash.

## Dev loop

From `editor/`:

```
npm run tauri dev
```

First run will download Tauri's Rust deps (a few minutes - only once). After
that, edits to the Svelte side hot-reload; edits to `src-tauri/src/*.rs`
trigger a Rust rebuild and a window restart.

## Build a portable ZIP (distribution)

The editor is shipped as an extract-and-run ZIP, not an installer. From the
repo root (or `editor/`):

```
npm run package:portable        # from editor/
# or directly:
pwsh -File tools\package-portable.ps1
```

This runs `tauri build --no-bundle`, then stages `Bosun.exe` next to the
installer assets (`circuitpython.uf2`, `firmware\`, `lib\`) and zips it to
`dist\Bosun-<version>-portable-x64.zip`. The recipient extracts it anywhere
and double-clicks `Bosun.exe` - no install, no admin rights. WebView2 ships
with Windows 11; older Windows needs the free Microsoft WebView2 runtime.

Run `tools\download-assets.ps1` once first so the assets exist under
`src-tauri/resources/`. Use `-SkipBuild` to repackage an existing
`target\release` build.

## Build a release binary (bare exe)

```
npm run tauri build -- --no-bundle
```

Output binary lands in `src-tauri/target/release/bosun-editor.exe`, with the
resource assets copied beside it. `tauri.conf.json` has `bundle.targets` set
to `[]`, so there is no installer target at all: even a plain `tauri build`
only produces the bare exe. The portable ZIP is the only distribution form.
App icons referenced by `tauri.conf.json` need to exist at
`src-tauri/icons/`.

## How the editor talks to the firmware

- Frontend (Svelte) calls Rust via `invoke()` - see `src/lib/protocol.ts`.
- Rust commands live in `src-tauri/src/serial.rs` and use the
  [`serialport`](https://crates.io/crates/serialport) crate to enumerate
  ports and open the firmware's secondary USB CDC.
- Firmware exposes the protocol on the **secondary** CDC port (not the REPL).
  On Windows you'll see two `COMx` entries when the pedal is plugged in -
  pick the one whose description matches the firmware's `usb_cdc.data`
  endpoint.
