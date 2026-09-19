# Morph controls and display

Use Bosun **0.6.9 or later** and matching native Captain firmware. Update the Pi
hub and Stage too if your setup uses a Pi. Prepare the Base and Morph sounds,
rise/fall times and optional Momentary setting on the Kemper or in Rig Manager.
Bosun controls the Morph position/button; it does not edit those sound parameters.

## Stage

A thin **BASE → MORPH** bar appears below the header for a Kemper profile on
supported native firmware. It stays separate from the VOL/WAH indicator.
Click or tap the bar to show its optional controls:

- **Base:** send position 0%.
- **Morph:** send position 100%.
- **Position:** choose 0–100%; the value is sent when you release the slider
  or commit its keyboard change.
- **Trigger Morph:** send a complete Morph Button tap, using the Kemper rig's
  button timing. For a held momentary Morph, use a physical footswitch.

The position controls emulate an expression pedal using **CC11, values 0–127**.
They do not use the button's rise/fall times. Trigger Morph sends **CC80 127,
then CC80 0**; these are press/release messages, not explicit Morph on/off.
See [Kemper's explanation of pedal and button control](https://forum.kemper-amps.com/forum/thread/67746-player-lvl-3-trouble-with-cc11-morph/).

## What the bar means

**Set 65%** means that Bosun last sent that pedal position successfully. It is
not measured progress, an acknowledgement from the Kemper, or confirmation that
the rig contains morphed parameters. MIDI's 128 positions are rounded to a whole
percentage for display.

**?** means the position cannot be established. Bosun clears the indication on
rig/profile changes, detected external Morph messages, a Morph Button command,
failed MIDI sends, lost Kemper communication and reconnection. It never animates
an invented rise/fall ramp. A control change that the Kemper does not report can
still leave Bosun's last command visible: **Set** must not be read as live state.

Kemper has confirmed that some Morph state feedback is intentionally omitted.
The pedal parameter can also retain a previous rig's value. Bosun therefore does
not poll that parameter or use it as confirmed Morph progress.
[Kemper's response about Morph feedback](https://forum.kemper-amps.com/forum/thread/64812-expose-morph-state-in-midi-sysex/).

Stage rejects commands for an old profile or rig generation and disables controls
while the Kemper rig is unavailable. A hub request for context after a Morph
command is ordered after that command.

## Captain footswitch

1. Open a Kemper profile and expand an unbound switch in the patch editor.
2. Choose **+ Morph Button**.
3. Save the patch. The shortcut creates a momentary Captain binding with paired
   **Morph Button Press** and **Release** actions. The Kemper's rig settings decide
   whether these toggle between sounds or behave momentarily.

For existing bindings, add **Morph Button (press/release)** messages to the
appropriate actions. Always pair a press with a release. The switch LED indicates
the button action; it does not confirm the current Morph sound.

For explicit positions in macros, choose **Set Morph Position** and enter
**Position (%)**. Existing saved `kemper_morph` values remain MIDI values 0–127;
the editor converts the percentage without changing the configuration format.

## Expression pedal and Captain screen

In **Settings → Expression pedals**, enable and calibrate the chosen jack, then
choose **Set Morph Position**. Heel is Base and toe is Morph; inversion and curves
remain available. Move the pedal to activate it. Patch-specific expression
overrides also support this message. Stage follows the positions sent by Captain.

In the Captain's **Screen** editor, add **Morph last commanded position (native)**
to your layout. It shows `SET 65%` or `?`. Existing screen layouts are preserved.

## Validation

Automated checks cover CC11/CC80 packets, expression input, failed sends, stale
requests, configuration changes, reconnects, unknown state, the Captain text
field and hub context ordering. Browser checks cover desktop, 800×480 HDMI and
phone layouts and controls using a simulated device. These checks do not establish
end-to-end operation on a physical Kemper; that hardware test remains pending.
