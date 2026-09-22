# Bank layout

Open **Settings → Banks** and set **Rigs per bank** (1–10) and **Number of banks**
(1–125), then click **Save settings**. Both settings belong to the active profile
and are included in configuration backups. Profiles without these settings use
five slots and allow all 125 banks. For a device with three rigs per bank and only
two banks in use, select three rigs and two banks in its Generic MIDI profile.

The Patches grid uses the selected number of columns. **New patch** and **Clone**
suggest the first free slot, moving to the next bank when those slots are full.
For example, after `01/01`, `01/02` and `01/03`, a three-rig profile suggests
`02/01`. Once all configured slots and banks are occupied, increase the layout
or free a slot before creating or cloning another patch.

**Number of banks** limits bank navigation to banks 1 through the chosen number.
With two populated banks, Bank Up cycles `1 → 2 → 1`; Bank Down wraps in the
opposite direction. Empty banks are skipped. The limit applies to the Captain's
bank-step and preview commands, Stage's previous/next buttons and its bank picker,
including Stage on Raspberry Pi. With only one available bank, Bank Up/Down does
not reload the current rig. With no patches inside the limit, it does nothing.

Lowering the limit does not change the playing patch. If its bank is above the
new limit, the next Bank Up selects the first available bank within the limit;
Bank Down selects the last. A pending preview outside the new limit is cancelled.

Reducing either setting does not move, delete or hide existing patches in the
editor. Higher slots and banks remain selectable there, but do not offer
new-patch placeholders.
Stage and the Captain's bank navigation already follow the patches that exist,
including preserved higher slots within the configured bank range. To make an existing five-rig profile
contain exactly three rigs per bank, reorganise those extra patches explicitly.

These settings describe the layout in Bosun; they do not change the target
device's MIDI protocol or rewrite saved Program Changes. In a Generic MIDI
profile, configure the target preset's Program Change (and Bank Select where
needed) in each patch's **On enter** action. Use **Preset navigation row** in
Settings to assign switches to rig slots within the current bank. Player and PROFILER Performance
MIDI addressing retain their five-rig structure. PROFILER Browse uses assigned
MIDI programs instead; see [PROFILER modes](kemper-head.md#choosing-rigs). Explicit patch selections, saved
MIDI actions, setlist sequences and following rig changes from the Kemper remain
available even for banks outside the navigation limit.

The settings are stored as `rigs_per_bank` and `bank_count` in the profile's
device configuration. Older firmware can store both through `PUT_GLOBAL`, but
**the Captain needs firmware supporting `bank_count` to enforce the limit on its
physical switches**. Update the editor and Stage as well; the Pi version receives
the limit through the firmware's compact device information. Changing only the
rigs-per-bank layout does not require a firmware update. Desktop and Android
share the same editor components.

Native firmware supports up to 625 configured patches across banks 1–125.
This is a catalog limit, not a promise of 125 banks of ten patches. Save in
batches when editing large setups: at most 128 unsaved drafts are supported.
