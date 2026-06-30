import serial, time
s = serial.Serial("COM4", 115200, timeout=0.5)
time.sleep(0.5)
if s.in_waiting:
    s.read(s.in_waiting)
s.write(b'{"type":"PING","id":"probe-test"}\n')
time.sleep(0.5)
data = s.read(2048)
s.close()
print("RAW BYTES:")
print(repr(data))
