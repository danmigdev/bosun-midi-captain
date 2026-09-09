# Stage configuration

Open **Stage** in Bosun Desktop or Android, or open the browser address described
in the [Raspberry Pi setup guide](../tools/rpi-hub/README.md).

![Stage rendered on the Raspberry Pi](ui-test-screenshots/stage_vnc.png)

Stage shows the playing rig, bank, expression mode and switch assignments.
Active effects and the selected rig have a stronger background colour. On an
interactive Stage page, tap or click a switch tile to operate its configured
action. The VNC viewer is read-only.

Use **+ / -** to browse banks or tap **BANK** to open the bank grid. See
[bank selection](stage-bank-selection.md) to choose immediate loading or
preselection.

## Appearance

The settings button beside VOL/WAH opens the appearance panel. Change fonts,
text sizes and colours for each section. Dropdown lists and the settings panel
scroll when needed. Text sizes default to **100%**, with a range of **10% to
500%**. The default is twice the original text size (formerly 200%). Reset
restores this new default; existing saved sizes are converted to the new
percentages so their visual size stays the same.

The **VOL / WAH** section controls the expression label's font, colour and size
(10%–500%). This indicator displays text only.

**Rounded corners** has one **Corners** slider shared by the screen frame (also
used by the bank picker), the two outer corners of the bottom switch row, and
the top-right corner of the main **X** button. It ranges from **0%** (square)
to **200%**, with **75%** as the default. Other button corners
keep their existing shape. Changes apply immediately and persist across reloads.
The group's **Reset** restores default rounding while preserving fonts and colours.

The outer frame has a small, softly pulsing highlight that travels clockwise
once every 24 seconds and follows the configured corners.

Preferences belong to the browser or app where they were saved. To configure
the HDMI kiosk's appearance, use its connected mouse or touch input. A separate
browser Stage page has its own settings; the VNC viewer cannot change them.

Close the appearance panel with **X**. In Desktop and Android, the main Stage
**X** returns to the editor; Android's Back action also exits Stage. In the
standalone browser and Pi kiosk, the main **X** reloads the Stage page.

## Raspberry Pi display

The [Pi installer](../tools/rpi-hub/README.md#install-on-the-pi) configures Stage
to start automatically over HDMI. The Pi uses the connected panel's preferred
resolution, or a 1920 x 440 output when no panel is attached. The Pi kiosk keeps
light bars steady to reduce rendering work.

Use the [VNC instructions](../tools/rpi-hub/README.md#view-the-actual-pi-display-on-windows)
to observe that actual display remotely. The separate browser display preview
renders on the viewing computer and is intended for checking a 1920 x 440 layout.
