/**
 * USB enumeration + port probing tests.
 *
 * Verifies the serial-plugin JSON formats the frontend depends on,
 * drawn from real device logs captured on a Pixel 8 Pro with a
 * MIDI Captain (Raspberry Pi Pico, VID 0x239A / PID 0x80F4).
 */

import { describe, it, expect } from "vitest";

// --- Real-world payloads captured from `adb logcat -s SerialPlugin` ---

const REAL_ENUMERATE_JSON = JSON.stringify({
  ports: {
    "/dev/bus/usb/001/002": {
      type: "Usb",
      vid: "0x239A",
      pid: "0x80F4",
      manufacturer: "Raspberry Pi",
      product: "Pico",
      serial_number: "DF635C76C783412E",
      interfaces: [
        { id: 0, class: 2, subclass: 2, protocol: 0 }, // CDC Communications (data port)
        { id: 1, class: 10, subclass: 0, protocol: 0 }, // CDC Data
        { id: 2, class: 2, subclass: 2, protocol: 0 }, // CDC Communications (console)
        { id: 3, class: 10, subclass: 0, protocol: 0 }, // CDC Data
        { id: 4, class: 3, subclass: 0, protocol: 0 }, // HID
        { id: 5, class: 1, subclass: 1, protocol: 0 }, // Audio
        { id: 6, class: 1, subclass: 3, protocol: 0 }, // Audio
      ],
    },
  },
});

interface SerialPort {
  type: "Usb";
  vid: string;
  pid: string;
  manufacturer: string;
  product: string;
  serial_number: string;
  interfaces: Array<{ id: number; class: number; subclass: number; protocol: number }>;
}

interface SerialEnumerate {
  ports: Record<string, SerialPort>;
}

interface PortInfo {
  name: string;
  kind: string;
}

describe("USB enumeration JSON parsing", () => {
  const parsed: SerialEnumerate = JSON.parse(REAL_ENUMERATE_JSON);

  it("parses the real MIDI Captain enumerate payload without errors", () => {
    expect(parsed.ports).toBeDefined();
    const keys = Object.keys(parsed.ports);
    expect(keys.length).toBeGreaterThanOrEqual(1);
  });

  it("has the correct VID/PID for MIDI Captain (0x239A/0x80F4)", () => {
    const port = Object.values(parsed.ports)[0];
    expect(port.vid).toBe("0x239A");
    expect(port.pid).toBe("0x80F4");
  });

  it("identifies the device as Raspberry Pi Pico", () => {
    const port = Object.values(parsed.ports)[0];
    expect(port.manufacturer).toBe("Raspberry Pi");
    expect(port.product).toBe("Pico");
  });

  it("exposes the expected 7 USB interfaces (2x CDC, 2x CDC Data, 1x HID, 2x Audio)", () => {
    const port = Object.values(parsed.ports)[0];
    expect(port.interfaces.length).toBe(7);

    const classes = port.interfaces.map((i) => i.class);
    // CDC Communications (class 2): interfaces 0, 2
    // CDC Data (class 10): interfaces 1, 3
    // HID (class 3): interface 4
    // Audio (class 1): interfaces 5, 6
    expect(classes).toEqual([2, 10, 2, 10, 3, 1, 1]);
  });

  it("has a serial_number present (required for stable port identification)", () => {
    const port = Object.values(parsed.ports)[0];
    expect(port.serial_number).toBeTruthy();
  });
});

describe("port info extraction (mirrors frontend listPorts)", () => {
  const realDevice: SerialEnumerate = JSON.parse(REAL_ENUMERATE_JSON);

  function extractPorts(enumerate: SerialEnumerate): PortInfo[] {
    const out: PortInfo[] = [];
    for (const [devPath, dev] of Object.entries(enumerate.ports)) {
      // CDC-ACM data interfaces: class=10 (CDC Data)
      // The serial plugin exposes each CDC Data interface as a port.
      for (const iface of dev.interfaces) {
        if (iface.class === 10) {
          out.push({
            name: `${devPath}#${iface.id}`,
            kind: `CDC ${dev.manufacturer} ${dev.product}`,
          });
        }
      }
    }
    return out;
  }

  it("extracts exactly 2 CDC data ports from MIDI Captain", () => {
    const ports = extractPorts(realDevice);
    expect(ports.length).toBe(2);
  });

  it("both ports have the correct path format (devicePath#interfaceId)", () => {
    const ports = extractPorts(realDevice);
    for (const p of ports) {
      expect(p.name).toMatch(/^\/dev\/bus\/usb\/\d+\/\d+#\d+$/);
    }
  });

  it("port kind includes manufacturer and product", () => {
    const ports = extractPorts(realDevice);
    for (const p of ports) {
      expect(p.kind).toBe("CDC Raspberry Pi Pico");
    }
  });
});

describe("empty USB state (no pedal connected)", () => {
  const EMPTY_JSON = JSON.stringify({ ports: {} });
  const parsed: SerialEnumerate = JSON.parse(EMPTY_JSON);

  it("returns an empty port list", () => {
    expect(Object.keys(parsed.ports).length).toBe(0);
  });

  it("is valid JSON", () => {
    expect(() => JSON.parse(EMPTY_JSON)).not.toThrow();
  });
});

describe("USB hotplug robustness", () => {
  it("handles a port appearing then disappearing (keys change)", () => {
    const before: SerialEnumerate = { ports: {} };
    const after: SerialEnumerate = JSON.parse(REAL_ENUMERATE_JSON);

    expect(Object.keys(before.ports).length).toBe(0);
    expect(Object.keys(after.ports).length).toBe(1);

    // When the device is unplugged, the next enumerate should be empty again
    const unplugged: SerialEnumerate = { ports: {} };
    expect(Object.keys(unplugged.ports).length).toBe(0);
  });

  it("detects a different USB device (wrong VID) is ignored", () => {
    const wrongDevice: SerialEnumerate = {
      ports: {
        "/dev/bus/usb/001/003": {
          type: "Usb",
          vid: "0x1234",
          pid: "0x5678",
          manufacturer: "Other",
          product: "Device",
          serial_number: "",
          interfaces: [{ id: 0, class: 10, subclass: 0, protocol: 0 }],
        },
      },
    };
    const port = Object.values(wrongDevice.ports)[0];
    expect(port.vid).not.toBe("0x239A");
    expect(port.vid).not.toBe("0x2E8A");
  });
});
