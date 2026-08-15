import time

import board
import digitalio
import storage
import usb_cdc
import usb_midi


def _switch_pressed_at_boot(pin):
    sw = digitalio.DigitalInOut(pin)
    sw.direction = digitalio.Direction.INPUT
    sw.pull = digitalio.Pull.UP
    time.sleep(0.3)
    readings = []
    for _ in range(5):
        readings.append(sw.value)
        time.sleep(0.02)
    sw.deinit()
    # print("  GP1 readings:", readings)
    return sum(1 for v in readings if not v) >= 3


# Force performance mode (USB MSC off, RW remount) regardless of GP1
# state.  The switch-debounce check is skipped entirely so a stuck or
# phantom GP1 reading can never trap the pedal in drive mode.
storage.disable_usb_drive()
storage.remount("/", readonly=False)

# Keep the console CDC enabled so print() output has somewhere to drain.
# Without it the internal buffer fills up during boot and print() blocks,
# slowing startup by seconds. The Android editor already avoids the console
# CDC (iface 0) by sorting ports in descending interface order, so nusb
# won't claim it and a DTR-triggered reset is not a concern.
usb_cdc.enable(console=True, data=True)
usb_midi.enable()
