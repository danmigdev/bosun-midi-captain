# Kemper PROFILER support (experimental)

Bosun 0.7.1 on `main` provides experimental support for the Kemper family:
**Player, Head, Rack and Stage**. Select **Kemper Player** for Player, or
**Kemper PROFILER — Head / Rack / Stage (experimental)** for the other models.
Head, Rack and Stage use one plugin and share the common Kemper engine with
Player. Physical validation of Head/Rack/Stage is still pending; this is an
experimental release, not a claim that every model/OS combination is verified.

## Setup

1. Use matching native Captain firmware, editor and Pi/Stage assets from 0.7.1.
2. Create a profile targeting **Kemper PROFILER — Head / Rack / Stage**.
3. In Settings, choose the MIDI channel used by the PROFILER.
4. In the PROFILER target section, select **Hardware generation** (`MK1` or
   `MK2`) and **PROFILER operating mode** (`performance` or `browse`). Select
   the same mode on the Kemper itself. These settings are manual: Bosun does
   not detect the hardware generation or switch the Kemper's operating mode.
5. Connect MIDI in both directions for rig names, effect feedback and tuner
   information. Test the controls below before using this experimental build live.

Older Head profiles default to MK1 and Performance Mode. Player profiles keep
using the Player plugin and their existing settings.

## Choosing rigs

| Mode | Bosun command | Kemper setup |
| --- | --- | --- |
| Performance | **Select Performance Slot**: Performance 1–125, slot 1–5 | Populate and enable the required slots in Performance Mode. |
| Browse | **Select Browse MIDI Program**: program 0–127 | Assign MIDI programs to rigs in the Kemper's Browse pool. Use the MIDI program number; some displays label these 1–128. |

Performance selection sends Bank Select plus Program Change, including all
625 slot addresses. Incoming Bank Select and Program Change reconstruct the
same identity. If a corresponding Bosun patch exists, an external Performance
selection follows it without transmitting its entry actions again.

Browse selection sends Program Change without Bank Select. Programs are
assignments, not positions in a sorted rig list. An external Browse change
updates the shared rig/effect state without automatically switching to an
unrelated Bosun patch. Put a Browse selection in each Bosun patch's **On enter**
action when you want that patch to select a particular assigned program.
**Step Rig / Performance** steps according to the mode selected on the Kemper.

Bosun bank coordinates now support **1–125** throughout native storage, editor,
Stage, hub snapshots and restore image generation. Existing two-digit paths
are preserved; banks 100-125 use three digits. Catalogs and active navigation
now support **625 configured patches**. Editor, Stage and update backups assemble
bounded pages and reject incomplete inventories. There is still a separate limit
of 128 unsaved drafts: save in batches when creating a large setup.

Configuration storage now reserves **4 MiB** on the 8 MiB Captain. Existing
512 KiB littlefs volumes mount without writes. Their original physical blocks
stay in place; the first configuration write atomically expands the filesystem.
Unknown or damaged volumes are never automatically formatted. Tests cover power
loss before, during and after each erase/program operation in the expansion.
625 representative patches were stored and read back using the real backend;
actual capacity still depends on patch sizes and other profiles sharing flash.

**Keep the full-flash backup made by Desktop before upgrading.** Once storage has
expanded, older firmware cannot mount it. To downgrade, restore that full-flash
backup; installing only an older UF2 is not a supported downgrade.

## Graphical setup and identity

Open the setup guide and select Player, Head, Rack or Stage, then USB or two
MIDI DIN cables for PROFILER models. All six host/display layouts are shared,
including Pi with HDMI and Pi with Android over Wi-Fi. The selected generation
and operating mode carry into profile creation. Existing profiles are unchanged.
DIN needs Captain OUT to Kemper IN and Kemper OUT to Captain IN for feedback;
leave Kemper USB disconnected in this layout to avoid a second MIDI route.

Settings offers **Check Kemper identity**, a read-only Universal Identity query
with a two-second reply window. Kemper manufacturer replies are shown as raw
family/member/revision bytes. There is no verified mapping from these bytes to
model, OS version, hardware generation or operating mode, so these remain manual.
No response is not proof that the device is incompatible. The probe never changes
capabilities or modes and does not send a Kemper OS update.

In Browse mode, Settings also offers an optional **Follow external Browse
programs** mapping. Map programs 0-127 to existing Bosun bank/slot destinations,
then save settings. Incoming mapped programs select the destination without
running entry actions; unmapped programs leave the Bosun patch unchanged.

## Controls and capabilities

- **Effect slots, tuner, Morph, wah and volume** use the shared engine and
  feedback handling. Effect/tuner feedback accepts both MIDI on values 1 and 127.
- **Fixed FX** is available for MK2 when supported by its installed Kemper OS.
  MK1 hides and rejects Fixed FX commands and skips fixed-wah queries. Choosing
  MK2 changes capabilities; it does not install Kemper features or firmware.
- **Looper** uses NRPN page 125, parameters 88–94 for Record/Play, Stop/Erase,
  Trigger, Reverse, Half Speed, Cancel Overdub and Erase. **Tap** is the default
  and sends press then release. Use **Press** and **Release** in matching
  footswitch actions when the Kemper function needs a held button.
- **Rotary Speed** uses CC33 (0 slow, 1 fast).
- **Set BPM** uses NRPN page 4, parameter 0, with Bosun's existing BPM scaling.
  Enable rig tempo on the Kemper when using tempo-synchronised effects.
- **Tap Tempo** sends a single CC30 value 0 event, avoiding a held tap button.

Looper, Rotary, Set BPM and Tap corrections apply to the common native engine,
including existing Player command IDs. A command being offered does not add a
feature absent from the connected Kemper or its OS. Morph displays the last
commanded position, not measured progress of the Kemper's Morph ramp.

Changing mode or generation cancels queued commands and resets the target
session. Commands inappropriate for the selected configuration are hidden in
new-action menus and rejected by the native runtime. Saved actions remain in
the configuration so the user can review or replace them.

## Shared implementation

[Model definitions](../firmware-native/plugins/kemper.json) hold model differences
and extra settings. The native build derives both plugins from shared schemas
and generates C model descriptors from the same definitions. MIDI transmission,
rig-change reconciliation, tuner, Morph and effect discovery are implemented
once. No Stage-specific engine or copied configuration is needed.

The internal profile ID remains `kemper_head` to preserve existing profiles and
backups. Both plugins retain shared `kemper_*` commands, the `device.kemper`
configuration block and the default screen layout. The frozen CircuitPython
firmware is unchanged.

For custom displays, context adds `kemper_mode` and `kemper_program` (0–127 in
Browse when the identity is known, otherwise -1). In Browse, bank/slot context
refers to the Bosun patch and `kemper_rig` is the one-based program identity.

## Validation still needed on hardware

Software tests cover model isolation, MK1/MK2 command gates, MIDI packet
encodings, Performance bank boundaries through slot 625, Browse program 127,
mode changes, three-digit bank save/reload, editor menus, and hub/Stage transport.
Host tests run with address/undefined-behaviour sanitizers. The RP2040 build
also checks firmware size, RAM usage and absence of dynamic allocation.

Before marking a specific Head, Rack or Stage/OS combination supported, verify:

1. Initial rig name/effects/tuner feedback and reconnect after unplugging MIDI.
2. Performance boundaries 128/129, 256/257, 512/513 and final slot 625.
3. Browse program assignments at both ends of the range and changes on the unit.
4. Rapid Clean → Crunch → Delay, including Captain LEDs and Bosun Stage state.
5. Looper press/release/hold, Rotary, Set BPM, Tap Tempo and Morph.
6. MK1 slot-wah detection, and MK2 Fixed FX on compatible OS.

No connected device is flashed by the build or automated tests.

References: [Kemper's model/OS FAQ](https://www.kemper-amps.com/faqs),
[Kemper-authored MIDI Parameter Documentation, June 2025](https://cdck-file-uploads-europe1.s3.dualstack.eu-west-1.amazonaws.com/flex017/uploads/electraone/original/2X/5/554852d1a4f562c1745150d36dcf83ce4402fe88.pdf),
and [PySwitch's Kemper model identifiers](https://github.com/Tunetown/PySwitch/blob/main/content/lib/pyswitch/clients/kemper/__init__.py).
