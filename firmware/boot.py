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
    print("  GP1 readings:", readings)
    return sum(1 for v in readings if not v) >= 3


_held = _switch_pressed_at_boot(board.GP1)
print("boot: FS1_held=" + str(_held))

if not _held:
    print("boot: performance mode (USB MSC off, RW remount)")
    storage.disable_usb_drive()
    storage.remount("/", readonly=False)
else:
    print("boot: editing mode (USB MSC on)")

usb_cdc.enable(console=True, data=True)
usb_midi.enable()
