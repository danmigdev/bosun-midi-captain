"""Capture every raw byte the firmware sends in response to GET_MANIFEST."""
import serial, time, sys

s = serial.Serial("COM4", 115200, timeout=0.05)
time.sleep(0.5)
while s.in_waiting:
    s.read(s.in_waiting)

s.write(b'{"type":"PING","id":"warm"}\n')
time.sleep(0.5)
s.write(b'{"type":"GET_MANIFEST","id":"mtest"}\n')
print("PING+MANIFEST sent. Reading 6 seconds...")
deadline = time.monotonic() + 6
total = bytearray()
chunks = 0
while time.monotonic() < deadline:
    chunk = s.read(2048)
    if chunk:
        chunks += 1
        total.extend(chunk)
        print(f"  read #{chunks}: {len(chunk)} bytes, total now {len(total)}")
s.close()
print(f"\nTotal bytes: {len(total)}, chunks: {chunks}")
print(f"Newline count: {total.count(b'\\n')}")
# Check for non-ASCII bytes
non_ascii = [b for b in total if b > 127]
print(f"Non-ASCII bytes: {len(non_ascii)}")
# Check for NULLs
nulls = total.count(b'\\x00')
print(f"NULL bytes: {nulls}")
# Show first 200 and last 100 bytes
print(f"\nFirst 200 bytes:\n  {total[:200]!r}")
print(f"\nLast 100 bytes:\n  {total[-100:]!r}")
