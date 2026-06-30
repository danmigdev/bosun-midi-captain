import supervisor
import usb_cdc

# Self-heal the blank-install case. When the editor copies the firmware
# onto a fresh pedal, CircuitPython does a soft reload (reruns code.py) but
# NOT boot.py, so the "data" USB CDC the editor talks to is never enabled
# and the editor cannot connect. boot.py only runs on a hard reset, so we
# force one here. Gated on AUTO_RELOAD: after the hard reset run_reason is
# STARTUP (not AUTO_RELOAD), so this can never loop; and editing files on an
# already-booted pedal won't trigger it because the data port is up by then.
if usb_cdc.data is None and supervisor.runtime.run_reason == supervisor.RunReason.AUTO_RELOAD:
    import microcontroller

    microcontroller.reset()

from captain.app import Captain

Captain().run()
