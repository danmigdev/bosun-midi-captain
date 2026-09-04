# bosun-hub

The Raspberry Pi appliance service. It owns the single connection to the
MIDI Captain's data USB-CDC port (the Bosun line-JSON protocol) and fans
it out to:

| Endpoint | Default | For |
|---|---|---|
| raw TCP | `:9876` | the editor, via its existing `tcp_connect` (`tcp://<pi>:9876`) |
| WebSocket | `:8081` | the on-Pi Stage kiosk browser |
| static HTTP | `:8080` | the built Stage kiosk bundle |

MIDI routing between the Captain and the Kemper Player is **not** done
here. That is a kernel-level ALSA sequencer connection (a udev +
`aconnect` rule), with no userspace process in the audio-critical path.
This service only carries the protocol/state channel that feeds the
display and the editor.

See `docs/plans/20260904_112618_rpi3_midi_hub_stage_display.md` for the
full design.

## Run

Auto-detect the Captain and serve the kiosk bundle:

```
python -m bosun_hub --stage-dir /opt/bosun-hub/stage
```

Point at an explicit port:

```
python -m bosun_hub --target /dev/ttyACM1
```

## Develop without a Pi or a pedal

```
python tools/tcp_firmware_emulator.py                 # terminal 1
cd tools/rpi-hub
python -m bosun_hub --target tcp://127.0.0.1:9876 \   # terminal 2
    --tcp-port 9899 --ws-port 8081 --http-port 8080
```

Then connect the editor to `tcp://127.0.0.1:9899`, or open a WebSocket to
`ws://127.0.0.1:8081`.

## Test

```
cd tools/rpi-hub
python tests/test_hub_e2e.py
python tests/test_server_smoke.py
# or, if pytest is available:
python -m pytest -q
```

The tests use `tests/fake_pedal.py`, a controllable TCP fake of the data
port (pushable CONTEXT lines, forced disconnects), so link sync, backlog
discard, keepalive, reconnect and fan-out all run with no hardware.

## Dependencies

`pyserial` and `websockets`. On Raspberry Pi OS install them as system
packages, no venv:

```
sudo apt install python3-serial python3-websockets
```

## Layout

```
bosun_hub/
  link.py     single-threaded owner of the data port: candidate discovery,
              PING/ACK sentinel sync, stale-backlog discard, keepalive,
              stall detection, hard reopen (ported from the Android
              serial backend's proven shape)
  hub.py      fan-out between the link thread and asyncio subscribers,
              bounded drop-oldest queues, HUB link-status frames
  server.py   the raw TCP / WebSocket / static HTTP front-ends
  __main__.py CLI
tests/
  fake_pedal.py       controllable data-port fake
  test_hub_e2e.py     link + hub behaviour
  test_server_smoke.py all three front-ends at once
```
