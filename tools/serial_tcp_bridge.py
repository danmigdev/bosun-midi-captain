"""
Serial-to-TCP bridge: forwards the real MIDI Captain (COM port)
to a TCP server so the Android emulator can connect via WiFi/localhost.

Usage: python serial_tcp_bridge.py COM4
Listens on 0.0.0.0:9876. Each TCP connection is bridged to the serial port.
"""

import serial
import socket
import sys
import threading
import time


class SerialTcpBridge:
    def __init__(self, serial_port: str, tcp_port: int = 9876):
        self.serial_port = serial_port
        self.tcp_port = tcp_port
        self.server = None
        self.running = False

    def _log(self, msg):
        print(msg, flush=True)

    def start(self):
        self.running = True
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("0.0.0.0", self.tcp_port))
        self.server.listen(1)
        self.server.settimeout(2.0)

        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        self._log(f"PC IP: {local_ip}")
        self._log(f"Serial: {self.serial_port}  <->  TCP: 0.0.0.0:{self.tcp_port}")
        self._log(f"On Android emulator, connect to 10.0.2.2:{self.tcp_port}")
        self._log("Waiting for connections...")

        try:
            while self.running:
                try:
                    client, addr = self.server.accept()
                    self._log(f"Client connected: {addr}")
                    client.settimeout(0.1)
                    t = threading.Thread(target=self._bridge, args=(client,), daemon=True)
                    t.start()
                except socket.timeout:
                    continue
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _bridge(self, client):
        ser = None
        try:
            ser = serial.Serial(self.serial_port, 115200, timeout=0.1)
            self._log(f"Serial {self.serial_port} opened OK")
        except Exception as e:
            self._log(f"Serial error: {e}")
            client.close()
            return

        try:
            while self.running:
                # Read from TCP client
                try:
                    data = client.recv(4096)
                    if data:
                        self._log(f"TCP->SER: {len(data)} bytes: {data[:100]}")
                        ser.write(data)
                    elif data == b'':
                        self._log("TCP client closed (EOF)")
                        break
                except socket.timeout:
                    pass
                except Exception as e:
                    self._log(f"TCP read error: {e}")
                    break

                # Read from serial
                try:
                    if ser.in_waiting > 0:
                        data = ser.read(ser.in_waiting)
                        if data:
                            self._log(f"SER->TCP: {len(data)} bytes: {data[:100]}")
                            client.sendall(data)
                except Exception as e:
                    self._log(f"Serial read error: {e}")
                    break

        except Exception as e:
            self._log(f"Bridge error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._log("Bridge thread closing")
            try:
                ser.close()
            except:
                pass
            try:
                client.close()
            except:
                pass

    def stop(self):
        self.running = False
        if self.server:
            try:
                self.server.close()
            except:
                pass


if __name__ == "__main__":
    port = sys.argv[1] if len(sys.argv) > 1 else "COM4"
    bridge = SerialTcpBridge(port)
    bridge.start()
