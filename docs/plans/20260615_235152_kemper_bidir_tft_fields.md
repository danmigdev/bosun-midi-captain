# Kemper bidirectional: rig name + BPM + tuner + connection status

Extends the `firmware/lib/plugins/kemper.py` plugin with four new TFT fields fed by the bidirectional beacon already active. All addresses verified against the PySwitch reference in `stock_backup_20260614_161517/lib/pyswitch/clients/kemper/`.

## Verified NRPN addresses

All SYSEX frames below are "payloads" without F0/F7 (the parser strips them). Layout:

```
data[0..2] = mfr (00 20 33)
data[3]    = product
data[4]    = device
data[5]    = function
data[6]    = instance (always 0x00)
data[7]    = page
data[8]    = addr
data[9..]  = value / payload
```

| Field | Function | Page | Addr | Payload |
|-------|----------|------|------|---------|
| Beacon sensing (already active) | `0x7E` | - | - | n/a, marks `confirmed=True` |
| Rig name | `0x03` (string response) | `0x00` | `0x01` | ASCII bytes from `data[9]` to `0x00` or end |
| BPM | `0x01` (single param response) | `0x04` (RIG_PARAMETERS) | `0x00` | 14-bit `(data[9] << 7) | data[10]`, BPM = `value / 64` |
| Tuner mode (SYSEX) | `0x01` | `0x7F` | `0x7E` | 14-bit value 0/1 |
| Tuner note | `0x01` | `0x7D` | `0x54` | 14-bit value, `value % 12` -> C/Db/D/Eb/E/F/Gb/G/Ab/A/Bb/B |
| Tuner deviance | `0x01` | `0x7C` | `0x0F` | 14-bit value, 8192 = in-tune, useful range ~8192 +/- 350 |

All these parameters live in PySwitch's `_PARAMETER_SET_2`; the beacon we already send (param set `0x02`) subscribes to them automatically.

## Implementation decisions

### Connection status (`kemper_connected`)
- Field with values `"on"` / `"off"`.
- `_BIDIR_STATE["confirmed"]` already exists but is an internal flag. Add `_BIDIR_STATE["published"]` to avoid calling `update_context` on every sensing frame (~2/s, the TFT refresh is expensive).
- Should `tick()` reset to `"off"` if no sensing arrives for X seconds? Not for now, simplify: once confirmed it stays confirmed until init is re-emitted. Can evolve later if needed.

### Rig name (`kemper_rig_name`)
- Tolerant parser: stop at first `0x00`, drop non-ASCII bytes (force `chr(b) if 0x20<=b<0x7F else ""`).
- Typical Kemper length: <= 16 chars, but no hard limit assumed: `data[9:]` until terminator.

### BPM (`kemper_bpm`)
- 14-bit `msb*128+lsb`, BPM display = `round(value / 64)`. PySwitch uses exactly this conversion (`mappings/tempo_bpm.py:convert_bpm`).
- Published as int, not string, so the TFT layout can apply prefix/suffix.

### Tuner (`kemper_tuner_note`, `kemper_tuner_deviance`)
- Note name: single string from a 12-tuple (`C, Db, D, Eb, E, F, Gb, G, Ab, A, Bb, B`).
- Deviance: published as `int` with nominal range 0..16383 centred on 8192. The TFT layout decides the representation (text-only for now; a graphical bar can come later).
- Tuner mode via SYSEX (page 0x7F, addr 0x7E) is authoritative: updates `kemper_tuner` like CC 31 already does. The two paths coexist: CC 31 is set manually by bosun via the `kemper_tuner` message, SYSEX is the bidirectional reply.

## Out of scope

- **Dedicated tuner splash UI**: pending TODO mentions "new TFT rendering mode". To minimise invasiveness and keep the existing "context + layout" model, just expose the fields. The user decides how to use them in the TFT layout (e.g. big note, deviance as `+/- N` prefix). A real tuner view (with a graphical bar) is a separate feature requiring changes to `display.py`.
- **Disabling CC 31 outbound when in tuner mode**: bosun keeps setting CC 31 when the user presses a `kemper_tuner` binding. The SYSEX broadcast is read-only.

## Test plan

`tools/bilateral_test.py`: add
- fn $03 rig name -> context update with a clean string
- fn $03 with non-ASCII bytes -> drops invalid bytes
- fn $01 page $04 addr $00 with MSB=2 LSB=0 (=256) -> BPM = round(256/64) = 4 (sentinel, real BPM lives in 40-250 range)
- fn $01 page $7D addr $54 with value=60 (mod 12 = 0) -> note "C"
- fn $01 page $7C addr $0F with value=8192 -> deviance 8192
- fn $01 page $7F addr $7E with value=1 -> kemper_tuner "on"
- `_BIDIR_STATE` connection status: $7E sensing flips published from off to on only once

## Version

Bump `firmware/lib/captain/__init__.py` VERSION to `0.3.0`, the new fields are an extension of the already-documented beacon protocol.
