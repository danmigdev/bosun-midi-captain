// Decoding for the firmware MIDI monitor. The pedal streams every inbound and
// outbound MIDI message as a "midi" EVENT carrying the raw framed bytes; the
// editor turns those bytes into a human-readable line here (kept firmware-side
// dumb on purpose, per the platform plan). Pure functions, no I/O, so this is
// unit-tested in isolation.

export type MidiDir = "in" | "out";

/** A "midi" EVENT as emitted by the firmware protocol layer. The index
 *  signature keeps it assignable to the generic `EVENT` firmware-message
 *  variant, so a type guard can narrow to it. */
export interface MidiMonitorEvent {
  type: "EVENT";
  event: "midi";
  dir: MidiDir;
  /** Present on inbound events: which physical port the bytes arrived on. */
  port?: string;
  /** Raw framed bytes: channel-voice (2-3 bytes) or a full F0..F7 SYSEX. */
  raw: number[];
  [k: string]: unknown;
}

export type MidiKind =
  | "note_off"
  | "note_on"
  | "poly_pressure"
  | "cc"
  | "program_change"
  | "channel_pressure"
  | "pitch_bend"
  | "sysex"
  | "unknown";

export interface DecodedMidi {
  kind: MidiKind;
  /** 1-16 for channel-voice messages, null for SYSEX / unknown. */
  channel: number | null;
  /** Short human label, e.g. "CC 7 (Volume) = 100" or "Note On C4 vel 96". */
  label: string;
  /** The raw bytes, echoed for the hex column. */
  raw: number[];
}

// A small, deliberately partial map of the CCs a guitarist actually reads in a
// monitor. Anything not listed just shows its number.
const CC_NAMES: Record<number, string> = {
  1: "Mod Wheel",
  4: "Foot",
  7: "Volume",
  11: "Expression",
  64: "Sustain",
  65: "Portamento",
  98: "NRPN LSB",
  99: "NRPN MSB",
  100: "RPN LSB",
  101: "RPN MSB",
};

const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

/** MIDI note number to name. Uses the C4 = 60 convention (Yamaha/"middle C"). */
export function noteName(n: number): string {
  const name = NOTE_NAMES[((n % 12) + 12) % 12];
  const octave = Math.floor(n / 12) - 1;
  return `${name}${octave}`;
}

export function toHex(raw: number[]): string {
  return raw.map((b) => b.toString(16).toUpperCase().padStart(2, "0")).join(" ");
}

/** Decode one raw framed MIDI message into a labelled record. Tolerant of short
 *  or malformed byte arrays: it degrades to a "unknown" kind rather than throw,
 *  so a garbled stream can never break the monitor. */
export function decodeMidi(raw: number[]): DecodedMidi {
  const unknown = (): DecodedMidi => ({ kind: "unknown", channel: null, label: `Raw ${toHex(raw)}`, raw });
  if (!raw || raw.length === 0) return { kind: "unknown", channel: null, label: "(empty)", raw: raw ?? [] };

  const status = raw[0];
  if (status === 0xf0) {
    // SYSEX: strip the F0/F7 framing for the byte count.
    const inner = raw.length >= 2 && raw[raw.length - 1] === 0xf7 ? raw.slice(1, -1) : raw.slice(1);
    return { kind: "sysex", channel: null, label: `SysEx (${inner.length} bytes)`, raw };
  }
  if (status < 0x80) return unknown(); // not a status byte

  const type = status & 0xf0;
  const channel = (status & 0x0f) + 1;
  const d1 = raw.length > 1 ? raw[1] : 0;
  const d2 = raw.length > 2 ? raw[2] : 0;

  switch (type) {
    case 0x80:
      return { kind: "note_off", channel, label: `Note Off ${noteName(d1)} vel ${d2}`, raw };
    case 0x90:
      // Running convention: Note On with velocity 0 is a Note Off.
      return d2 === 0
        ? { kind: "note_off", channel, label: `Note Off ${noteName(d1)} (vel 0)`, raw }
        : { kind: "note_on", channel, label: `Note On ${noteName(d1)} vel ${d2}`, raw };
    case 0xa0:
      return { kind: "poly_pressure", channel, label: `Poly Pressure ${noteName(d1)} = ${d2}`, raw };
    case 0xb0: {
      const name = CC_NAMES[d1];
      return { kind: "cc", channel, label: `CC ${d1}${name ? ` (${name})` : ""} = ${d2}`, raw };
    }
    case 0xc0:
      return { kind: "program_change", channel, label: `Program Change ${d1}`, raw };
    case 0xd0:
      return { kind: "channel_pressure", channel, label: `Channel Pressure ${d1}`, raw };
    case 0xe0: {
      const value = (d2 << 7) | d1; // 14-bit, 0..16383, center 8192
      return { kind: "pitch_bend", channel, label: `Pitch Bend ${value - 8192}`, raw };
    }
    default:
      return unknown();
  }
}
