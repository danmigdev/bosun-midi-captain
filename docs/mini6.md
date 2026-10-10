# MIDI Captain Mini 6

The Bosun firmware also runs on the 6-switch **PaintAudio MIDI
Captain Mini 6**. Support is **experimental and untested on real hardware**: it
follows the community pin map published by
[PySwitch](https://github.com/Tunetown/PySwitch/blob/main/content/lib/pyswitch/hardware/devices/pa_midicaptain_mini_6.py).
If you own a Mini 6, your feedback decides when this becomes a supported model.

The 10-switch MIDI Captain (STD) is unaffected: it keeps working exactly as
before, and it remains the default whenever a pedal has not been told its model.

## What Bosun knows about the Mini 6

| | MIDI Captain (10 switches) | MIDI Captain Mini 6 |
| --- | --- | --- |
| Footswitches | top row 1, 2, 3, 4, UP; bottom row A, B, C, D, DOWN | top row 1, 2, 3; bottom row A, B, C |
| Switch LEDs | 30 (3 per switch) | 18 (3 per switch) |
| Display | 240x240 | 240x240 (same driver and pins in PySwitch) |
| Expression jacks | 2 | none (not defined by PySwitch) |
| Flash | 8 MiB, verified | unknown: Bosun requires 8 MiB |

The Mini 6 switches 1, 2, 3, A, B and C use the same RP2040 pins as the
switches with the same names on the 10-switch Captain, and the LEDs, display
and MIDI use the same pins too. That is why one firmware serves both models:
the pedal stores which model it is, and every part of Bosun (editor, Stage,
Raspberry Pi display) draws the matching layout.

## Install Bosun on a Mini 6

Follow the normal [first installation](first-setup.md#2-install-bosun-firmware-on-the-captain)
and choose **MIDI Captain Mini 6** in the installer. Before writing anything,
the installer saves and verifies a complete backup and checks the flash chip:
on a pedal without 8 MiB of flash it stops with "does not report the 8 MiB
flash chip Bosun requires" and nothing is written. After installing, it stores
the model on the pedal and checks that the pedal reports it.

If that last step does not complete, Bosun still finishes the installation and
asks **Which MIDI Captain is this?** the first time you connect, before you
create your first profile.

## Change the model

Open **Maintenance → Pedal model**, choose your model and select **Apply and
restart**. The pedal restarts and Bosun reconnects. Profiles, patches and
settings are not touched. A wrong choice does no harm: switches and LEDs are
just mapped incorrectly until you pick the right model.

The model is stored on the pedal outside every profile, so it survives
firmware updates, Raspberry Pi updates and profile changes, and configuration
backups never carry it to another pedal.

## Moving a configuration from a 10-switch Captain

Backups and profiles made on a 10-switch Captain restore normally on a Mini 6.
Entries for switches the Mini 6 does not have (4, UP, D, DOWN) are kept but
never fire:

- the patch editor lists them under **Not on this pedal**, with a button to
  remove each binding;
- Settings lists navigation, long-press and expression entries for missing
  switches and jacks in the same way, each with a **Remove** button;
- the firmware keeps copied expression settings switched off and never drives
  the expression pins. Profiles created on a Mini 6 contain no expression
  settings at all.

Bank navigation on the 10-switch Captain often uses UP and DOWN. On a Mini 6,
assign **Step Captain Bank** (or rig stepping) to the switches you want, or use
long-press actions.

## Recovery and bootloader

Holding **switch 1 (top-left)** alone for three seconds at power-on enters the
RP2040 bootloader (RPI-RP2) on both models; **Maintenance → Enter bootloader**
works too. PaintAudio's own Mini 6 bootloader and recovery procedure has not
been verified with Bosun: keep the installer's backup.

## Help test it

If you try Bosun on a Mini 6, please report:

- whether the installer accepted the flash chip, or the exact message it showed;
- whether each switch triggers the binding shown under the same name in the editor;
- whether each switch's LEDs light with the colour of that switch;
- whether the display is oriented correctly.

## Technical reference

- Record: `/config/hardware.json` in the pedal storage, `{"version":1,"model":"mini6"}`.
  A missing or unreadable record means the 10-switch model.
- `DEVICE_INFO.hardware`: `model`, `name`, `configured` (a record selected the
  model), `switches` (chain order), `rows` (top row first), `led_count`,
  `expression_jacks` and `models` (accepted by `SET_HARDWARE`).
- `SET_HARDWARE {"model":"mini6"}` replies `ACK` with `model` and `reboot`; a
  different model takes effect after a normal restart.
- `CONTEXT.switches`, `hold_mask`, `binding_fired` and `LED_DUMP` use the
  pedal's own switch names and order.
- To emulate a Mini 6, write the record into the emulator's `--root` directory
  (`config/hardware.json`) before starting `bosun_emulator`.
