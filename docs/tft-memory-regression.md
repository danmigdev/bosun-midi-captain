# Captain TFT row loss and expression indicator

Reproduced on the real RP2040 Captain / CircuitPython 9.2.7, 2026-09-05.

The configured layout contains patch title, BANK, RIG and hold-effect rows.
While CLEAN showed only its title, a correlated GET_CONTEXT still contained
`bank=1`, `slot=2` and `kemper_rig_in_bank=2`. Repeated CLEAN/CRUNCH changes
produced console errors allocating 136–232 bytes inside BANK/RIG Label creation.
The renderer rebuilt a complete Group while the previous one was still live,
caught each allocation error as a bad layout entry, and installed the incomplete
Group. This is a reproduced rendering failure, not evidence of missing bank data.
The same capture also contained `_send EXC type=CONTEXT err=MemoryError`.

The renderer now retains ordinary screen objects. System-font text updates a
TileGrid using terminalio's built-in glyph atlas; custom fonts retain the
Adafruit renderer. Layout allocation failures retain the previous complete
screen and schedule a deferred retry. System labels preserve previous text
when growth or Unicode glyph resolution fails. The pedal icon and VOL/WAH are
an independent `expression_mode` layout element, with ordinary alignment,
position, font, size and color properties; unconfirmed state is shown as `---`.
The title no longer reserves space or reduces its configured font size. The
new-profile default places the indicator at the bottom-right of the TFT.

`tools/display_test.py` reproduces the old missing-row failure and rejects the
old allocation pattern. Its 1,000-change test forbids new graphics objects and
ASCII glyph objects after the first render. It also covers anchors, shorter and
empty strings, Unicode, multiline text, scrolling, tuner, preview and badge.
The real-hardware TFT test completed 30 changes across five rigs without console
memory/render/send errors or increased MIDI TX drops. Those checks observe
software state; they do not replace visual inspection of the physical TFT.

Independent ALSA capture confirmed the Player's fixed-Wah state at page 5,
address 21 decimal. An NRPN change to ON produced no spontaneous state broadcast
in the test; a subsequent read returned `F0 00 20 33 00 00 01 00 05 15 00 01 F7`.
Therefore query-once was insufficient. The indicator polls after rig settling,
500 ms after each response, with one request outstanding. Missing replies clear
stale mode, and repeated misses use a five-second backoff. Rig changes quarantine
older replies; unchanged responses do not publish another context update.

The original fixed-block-only implementation was insufficient: on the real
CRUNCH B1/R3, fixed Wah was OFF but slot A contained an active Wah Wah.
Independent readback captured `F0 00 20 33 00 00 01 00 32 00 00 01 F7` (type 1)
and `F0 00 20 33 00 00 01 00 32 03 00 01 F7` (slot A ON). Showing VOL in this
case was a state-source bug, not a display-placement problem. The revised
indicator aggregates the fixed block and recognized Wah-family slot effects;
VOL requires confirmation that no such source is active.

The indicator does not alter expression MIDI routing or prove the Kemper's
WahPedal-to-Volume setting. If Captain has no patch
for the selected rig and its identity cannot be reconciled, mode stays unknown.

Reproducible hardware commands are documented in `tools/rpi-hub/README.md`.
The broader Stage/GPIO/physical LED capture gaps remain separate: this finding
does not by itself demonstrate the cause of every earlier Stage mismatch.

Earlier fixed-block polling-firmware validation: 10/10 fixed-Wah transitions matched independent
Kemper readback (587–808 ms including test transport); initial OFF was restored.
Five rig and five effect cycles passed in the browser while ten 33-request CDC
bursts ran concurrently. Earlier display-fix validation also passed 20 rig and
20 effect cycles. The full offline battery passed 26 suites.

An additional TFT probe later timed out on GET_CONTEXT. Its original restoration
reader also failed because Python socket.makefile cannot be reused after a read
timeout. A fresh connection confirmed CLEAN B1/R2, X on, Reverb off, VOL. The
intermittent protocol timeout is still unresolved; these results are not a claim
that all system reliability issues have been eliminated. The MIDI discarded-byte
counter was 7 on that boot; its initial value was not recorded, so origin is
unknown. It remained 7 during a subsequent ten-burst diagnostic test.

The final deploy attempt initially hit CircuitPython HARD_FAULT safe mode during
the script's unconditional USB bus reset, before uploading the new plugin. A
normal console reset restored both CDC interfaces; protocol upload and bootstrap
verification then succeeded. The deployment script now tries PING first and
does not bus-reset a healthy Captain. This avoids that observed failing step;
it does not establish the native runtime's internal hard-fault mechanism.

The follow-up local verification found stale `display.mpy`, `kemper.mpy`,
`manifest-tail.json` and packaged firmware resources despite the newer source
changes. Rebuilding with the pinned CircuitPython compiler and regenerating the
manifest restores the independent bottom-row indicator in the shipped defaults.
The resulting bytecode passes the complete reproducible-build comparison.

Slot-Wah polling now also participates in the effect-query quarantine at rig
changes. A delayed reply, including one left behind after a successful retry,
must not become a confirmation for the next rig's effect or LED. The offline
Kemper suite covers actual discovery requests, CRUNCH's slot Wah, bypass and
reenable, and both delayed-reply cases (148 checks). TFT/editor checks and 27
Chromium geometry cases on nine viewports also pass. These follow-up checks are
local; they do not constitute a new Captain or Raspberry Pi deployment.

Subsequent live validation reproduced another CRUNCH failure even with that
bytecode installed. CircuitPython iterated `_EFFECT_CC` in the order
`Delay, D, Reverb, Mod, A, X, C, B`, whereas the Wah type-page table used
`A, B, C, D, X, Mod, Delay, Reverb`. The inferred slot index consequently paired
A's Wah type with Delay's OFF state. Independent MIDI capture showed type A=1
and A ON=1 while the firmware repeatedly requested Delay at page 74/address 2.
The slot order is now explicit. A regression reorders the dictionary before
loading the module and exercises a Wah in every slot: 16 checks failed before
the fix; all 164 Kemper checks pass afterwards.

The corrected plugin was deployed to the Captain. A direct TCP read through
the Raspberry Pi returned CRUNCH B1/R3, `kemper_block_A=on`, `expression_mode=WAH`;
the deployed Stage also showed WAH beside its new lime gradient separator.
The deploy's immediate bootstrap check encountered `background_busy` while
clients were reconnecting; a subsequent standalone runtime verification passed.

Screen Layout saves also exposed an inbound allocation peak: the receiver kept
the complete raw PUT_GLOBAL line while decoding a second configuration tree.
A failed buffer extension could pin the pending suffix indefinitely; a parser
MemoryError could also lose the request id. Long input now spills to one owned
temporary file, releases its raw buffer before json.load, and drains a failed
line without blocking the next command. USB reads and spool writes are bounded
to 256 bytes. RX uses a buffer reserved at startup and nonblocking readinto;
it no longer falls back to reading one byte per main-loop tick when a fresh
USB-read allocation fails. Byte-wise buffer growth also avoids retrying an
atomic extend at offset zero indefinitely. A regression receives a roughly
2.5 KB PUT_GLOBAL and the following PING within twelve poll calls under those
simulated allocation failures. New editor requests place type/id before the payload. Save allows
12 seconds for its ACK and verifies GET_GLOBAL once after a timeout without
repeating the write. LIST_FONTS now streams its response as well.

A live full-configuration save (2421 JSON bytes before the hub's compact
encoding) returned ACK in 3.547 seconds and exact readback. The native desktop
Screen Save also returned ACK and global_changed, displayed its confirmation,
and retained the complete configuration. An initial post-upload probe still
timed out and firmware uploads encountered CDC stalls, so those successes do
not establish that every intermittent transport failure is eliminated.

Stage now takes title, BANK, RIG and expression colors from the Screen layout.
BANK/RIG use its field values and saved prefixes/suffixes instead of fixed B/R
abbreviations. Compact DEVICE_INFO projections tft_colors and tft_labels let
the kiosk do this without fetching the whole configuration; global_changed
refreshes them after a save. VOL uses a crescendo triangle and WAH a pedal
side profile in Stage, the editor preview and the TFT.

The OTA handlers now live in captain_ota, loaded only during file uploads and
released afterwards. The compiler inventory includes this helper, and both
desktop and Python upload listings install it before protocol and application
roots so an interrupted update retains the ability to resume OTA.

A later native Screen Save still timed out; the passive console and REPL
reported `supervisor.runtime.safe_mode_reason == HARD_FAULT`. The changed
test color was already on disk. Safe mode had skipped boot.py, explaining
the reappearing USB mass-storage interface and read-only filesystem. Those
observations distinguish a runtime reset from an ordinary slow JSON response.

The [CircuitPython 9.2.7 array implementation](https://github.com/adafruit/circuitpython/blob/9.2.7/py/objarray.c#L427-L480)
contains a concrete corruption hazard in bytearray.append: it sets the free
capacity before attempting reallocation. If that allocation raises
MemoryError, a retry can trust capacity that was never allocated and write
past the buffer. Extending with a buffer updates capacity only after a
successful reallocation; extending with an iterable still falls back to the
unsafe append path. Captain now reserves a one-byte bytearray at startup and
uses only extend with that buffer for RX growth. Pending offsets, bounded
readinto, spooling and failed-frame draining are preserved.

The offline regression models append poisoning the buffer and one-byte
extend failing atomically, verifies recovery of the complete PUT_GLOBAL and
following PING, and forbids any RX append. A separate 2.5 KB throughput case
still completes within twelve polls with one reused USB buffer and one reused
octet buffer. All 144 protocol checks and 50 stability checks pass. These
checks verify the workaround; a fresh hardware save is required to establish
whether it removes the observed hard fault.

The safe RX build removed the observed reset, but a changed layout exposed a
second allocation peak: recreating the complete native TFT group retained the
old frame while allocating its replacement. Presentation edits now reuse the
existing groups, text grids and badge palettes. Position, scale, field text and
colors can change without a second screen; row count, font and label-type
changes still rebuild. Failed color updates restore prior palettes and remain
retryable. All 28 display checks and 50 stability checks pass; the safe RX soak
completed 60,000 iterations with no queued CDC input or object growth beyond
one retained object.

Live validation of the combined firmware passed a changed-color PUT_GLOBAL
(3.110 seconds), exact GET_GLOBAL readback, restoration (2.656 seconds), and
exact full-configuration readback. Console capture reported no deferred TFT
refresh or loop error. Native desktop Screen Save then passed both changed
color and restore, including asynchronous readback (7.927 and 7.429 seconds).
The final full configuration equals the initial snapshot. The first native
driver checked its asynchronous readback too early; waiting for the expected
value confirmed both ACKed writes rather than mistaking stale editor state
for failed storage. CLEAN B1/R2 remains active. Native Stage reads CLEAN,
BANK 1, RIG 2 and VOL with the saved white/gray/green/white colors.

The native Stage check also exercised the user's enlarged UI (24 px root font)
and a nearly square landscape window. Height-only font limits caused ordinary
saved names to scroll. Landscape font limits now consider viewport width too,
while preserving theme multipliers and scrolling for genuinely long names.
All 62 Stage component checks and 31 browser geometry cases pass, including
four enlarged-UI viewports. The updated Stage bundle was deployed to the Pi.

An idempotent OTA-helper smoke exposed an import allocation failure before
PUT_FILE_BEGIN could open its staging file. The protocol now suspends the
cached TFT frame before loading the helper. Rendering remains suspended until
the last open upload ends; success, failure and disconnect unload the helper
and schedule a fresh frame. Import itself is inside the cleanup boundary.
No configuration or MIDI state changes during this temporary display pause.
The renderer dependency is installed before protocol. The final offline
checks cover import failure and multiple simultaneous uploads (146 protocol,
29 display and 50 stability checks).

Final hardware/package verification: the 1,712-byte OTA helper was reuploaded
through the Raspberry TCP route in 6.672 seconds, with ACKed PING/STATS after
TFT resume (free heap before/after 4,112/4,016 bytes). The final native desktop
then saved a changed color and restored the initial full configuration with
no errors; including readback, the two operations took 7.615 and 6.345 seconds.
Stage showed CLEAN / BANK 1 / RIG 2 with the saved colors, readable full labels,
VOL aligned right, the lime separator, and complete lower block borders.
All 26 offline firmware suites passed on these final sources. Captain readback
SHA-256 matches display.mpy and protocol.mpy; the portable directory and ZIP
both passed inventory/hash verification for all 110 packaged firmware files.
