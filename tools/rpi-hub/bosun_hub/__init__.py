"""
bosun-hub: the Raspberry Pi appliance service.

Owns the single connection to the MIDI Captain's data USB-CDC port
(the Bosun line-JSON protocol) and fans it out to many consumers:

  - a raw TCP listener on :9876, wire-compatible with the editor's
    existing ``tcp_connect`` transport (editor/src-tauri/src/tcp_serial.rs)
  - a WebSocket listener for the on-Pi Stage kiosk browser
  - a static HTTP server for the Stage kiosk bundle

MIDI routing between the Captain and the Kemper Player is NOT done here:
that is the Linux ALSA sequencer's job (a udev + ``aconnect`` rule), in
kernel, with no userspace process in the path. This service only carries
the protocol/state channel that feeds the display and the editor.
"""

__version__ = "0.1.0"
