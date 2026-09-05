package com.bosun.app

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Local JVM unit tests for [UsbMidiPacketCodec] - no emulator/device needed
 * (`./gradlew testArmDebugUnitTest`). This codec is the part of the
 * android.media.midi bypass that MUST be correct: a framing bug here would
 * corrupt or drop MIDI silently, trading one reliability problem (the
 * Android framework's silent callback death) for another of our own making.
 */
class UsbMidiPacketCodecTest {

    private fun b(vararg ints: Int) = ByteArray(ints.size) { ints[it].toByte() }
    private fun u(byte: Byte) = byte.toInt() and 0xFF

    // ---------------- encode: channel voice messages ----------------

    @Test
    fun `note on encodes to a single CIN 9 packet`() {
        val packets = UsbMidiPacketCodec.encode(b(0x90, 0x40, 0x7F))
        assertEquals(1, packets.size)
        assertArrayEquals(b(0x09, 0x90, 0x40, 0x7F), packets[0])
    }

    @Test
    fun `note off on channel 16 encodes with the right channel nibble`() {
        val packets = UsbMidiPacketCodec.encode(b(0x8F, 0x30, 0x00))
        assertArrayEquals(b(0x08, 0x8F, 0x30, 0x00), packets[0])
    }

    @Test
    fun `poly aftertouch, CC, pitch bend are 3-byte CIN A B E`() {
        assertArrayEquals(b(0x0A, 0xA1, 0x10, 0x50), UsbMidiPacketCodec.encode(b(0xA1, 0x10, 0x50))[0])
        assertArrayEquals(b(0x0B, 0xB2, 0x07, 0x64), UsbMidiPacketCodec.encode(b(0xB2, 0x07, 0x64))[0])
        assertArrayEquals(b(0x0E, 0xE0, 0x00, 0x40), UsbMidiPacketCodec.encode(b(0xE0, 0x00, 0x40))[0])
    }

    @Test
    fun `program change and channel pressure are 2-byte CIN C D, third byte zero-padded`() {
        assertArrayEquals(b(0x0C, 0xC3, 0x05, 0x00), UsbMidiPacketCodec.encode(b(0xC3, 0x05))[0])
        assertArrayEquals(b(0x0D, 0xD4, 0x60, 0x00), UsbMidiPacketCodec.encode(b(0xD4, 0x60))[0])
    }

    @Test
    fun `cable number is packed into the header's high nibble, CIN untouched`() {
        val packets = UsbMidiPacketCodec.encode(b(0x90, 0x40, 0x7F), cable = 5)
        assertEquals(0x59, u(packets[0][0]))  // cable 5 << 4 | CIN 9
    }

    // ---------------- encode: system common / real-time ----------------

    @Test
    fun `system common 2-byte and 3-byte messages`() {
        // MTC quarter frame (0xF1) - CIN 2, 2 bytes.
        assertArrayEquals(b(0x02, 0xF1, 0x03, 0x00), UsbMidiPacketCodec.encode(b(0xF1, 0x03))[0])
        // Song position pointer (0xF2) - CIN 3, 3 bytes.
        assertArrayEquals(b(0x03, 0xF2, 0x10, 0x20), UsbMidiPacketCodec.encode(b(0xF2, 0x10, 0x20))[0])
    }

    @Test
    fun `tune request is a single-byte CIN 5 message`() {
        assertArrayEquals(b(0x05, 0xF6, 0x00, 0x00), UsbMidiPacketCodec.encode(b(0xF6))[0])
    }

    @Test
    fun `real-time bytes (clock, active sensing, start, stop, continue, reset) are CIN F`() {
        for (status in listOf(0xF8, 0xFA, 0xFB, 0xFC, 0xFE, 0xFF)) {
            val packets = UsbMidiPacketCodec.encode(b(status))
            assertArrayEquals("status 0x%02X".format(status), b(0x0F, status, 0x00, 0x00), packets[0])
        }
    }

    // ---------------- encode: malformed / edge input ----------------

    @Test
    fun `empty message encodes to nothing, does not throw`() {
        assertTrue(UsbMidiPacketCodec.encode(ByteArray(0)).isEmpty())
    }

    @Test
    fun `a bare data byte with no status encodes to nothing, does not throw`() {
        assertTrue(UsbMidiPacketCodec.encode(b(0x40, 0x7F)).isEmpty())
    }

    @Test
    fun `a short channel voice message is zero-padded, not out of bounds`() {
        // CC needs 3 bytes; only 2 supplied.
        val packets = UsbMidiPacketCodec.encode(b(0xB0, 0x07))
        assertArrayEquals(b(0x0B, 0xB0, 0x07, 0x00), packets[0])
    }

    // ---------------- encode: SysEx ----------------

    @Test
    fun `sysex exactly 2 bytes (F0 F7) fits one CIN 6 packet`() {
        val packets = UsbMidiPacketCodec.encode(b(0xF0, 0xF7))
        assertEquals(1, packets.size)
        assertArrayEquals(b(0x06, 0xF0, 0xF7, 0x00), packets[0])
    }

    @Test
    fun `sysex exactly 3 bytes fits one CIN 7 packet`() {
        val packets = UsbMidiPacketCodec.encode(b(0xF0, 0x41, 0xF7))
        assertEquals(1, packets.size)
        assertArrayEquals(b(0x07, 0xF0, 0x41, 0xF7), packets[0])
    }

    @Test
    fun `sysex 4 bytes splits into a CIN 4 continue and a CIN 5 tail`() {
        val packets = UsbMidiPacketCodec.encode(b(0xF0, 0x41, 0x42, 0xF7))
        assertEquals(2, packets.size)
        assertArrayEquals(b(0x04, 0xF0, 0x41, 0x42), packets[0])
        assertArrayEquals(b(0x05, 0xF7, 0x00, 0x00), packets[1])
    }

    @Test
    fun `real kemper-shaped beacon sysex round-trips through encode+decode`() {
        // f0 00 20 33 02 7f 7e 00 40 02 23 05 f7 - shape observed live on
        // the pedal's beacon SysEx (12 payload bytes, seen truncated in the
        // Android bridge log at 12 bytes without the F7 - this is the full,
        // correctly-terminated form).
        val msg = b(0xF0, 0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02, 0x23, 0x05, 0xF7)
        val packets = UsbMidiPacketCodec.encode(msg)
        val decoder = UsbMidiPacketCodec.Decoder()
        val decoded = ArrayList<ByteArray>()
        for (p in packets) decoded.addAll(decoder.feed(p))
        assertEquals(1, decoded.size)
        assertArrayEquals(msg, decoded[0])
    }

    @Test
    fun `long sysex (100 bytes) round-trips`() {
        val msg = ByteArray(100)
        msg[0] = 0xF0.toByte()
        for (i in 1 until 99) msg[i] = (i and 0x7F).toByte()
        msg[99] = 0xF7.toByte()
        val packets = UsbMidiPacketCodec.encode(msg)
        val decoder = UsbMidiPacketCodec.Decoder()
        val decoded = ArrayList<ByteArray>()
        for (p in packets) decoded.addAll(decoder.feed(p))
        assertEquals(1, decoded.size)
        assertArrayEquals(msg, decoded[0])
    }

    @Test
    fun `encode always terminates the last group, even without a trailing F7 byte`() {
        // encode() frames whatever bytes it's given as a complete message -
        // it doesn't police the caller for a literal trailing 0xF7. The
        // terminating CIN (5/6/7) just means "this is the last packet of
        // this SysEx", independent of the actual byte values, so this
        // round-trips exactly like any other SysEx.
        val msg = b(0xF0, 0x01, 0x02, 0x03, 0x04)  // no trailing F7
        val packets = UsbMidiPacketCodec.encode(msg)
        val decoder = UsbMidiPacketCodec.Decoder()
        val decoded = ArrayList<ByteArray>()
        for (p in packets) decoded.addAll(decoder.feed(p))
        assertEquals(1, decoded.size)
        assertArrayEquals(msg, decoded[0])
    }

    @Test
    fun `a sysex whose packets never reach a terminator emits nothing (no premature flush)`() {
        // Genuine "still in flight" case: only the CIN-4 continuation
        // packets have arrived so far (e.g. the terminator is in the next,
        // not-yet-delivered, bulk transfer) - must not emit a partial
        // message early.
        val msg = ByteArray(20) { (it + 1).toByte() }.let { byteArrayOf(0xF0.toByte()) + it + byteArrayOf(0xF7.toByte()) }
        val packets = UsbMidiPacketCodec.encode(msg)
        assertTrue("test needs a multi-packet sysex", packets.size >= 2)
        val decoder = UsbMidiPacketCodec.Decoder()
        val decoded = ArrayList<ByteArray>()
        for (p in packets.dropLast(1)) decoded.addAll(decoder.feed(p))  // withhold the terminator
        assertTrue(decoded.isEmpty())
    }

    // ---------------- decode: framing / reassembly ----------------

    @Test
    fun `decoder reassembles a sysex split across two separate feed() calls`() {
        // Simulates a USB bulk transfer boundary landing mid-SysEx.
        val msg = b(0xF0, 0x01, 0x02, 0x03, 0x04, 0x05, 0xF7)
        val packets = UsbMidiPacketCodec.encode(msg)
        assertTrue("test needs a multi-packet sysex", packets.size >= 2)
        val decoder = UsbMidiPacketCodec.Decoder()
        val mid = packets.size / 2
        val firstHalf = packets.take(mid).flatMap { it.toList() }.toByteArray()
        val secondHalf = packets.drop(mid).flatMap { it.toList() }.toByteArray()
        val decoded = ArrayList<ByteArray>()
        decoded.addAll(decoder.feed(firstHalf))
        assertTrue("should not complete on the first half alone", decoded.isEmpty())
        decoded.addAll(decoder.feed(secondHalf))
        assertEquals(1, decoded.size)
        assertArrayEquals(msg, decoded[0])
    }

    @Test
    fun `a real-time byte interleaved mid-sysex is emitted on its own and does not corrupt the sysex`() {
        val sysex = b(0xF0, 0x01, 0x02, 0x03, 0x04, 0xF7)
        val sysexPackets = UsbMidiPacketCodec.encode(sysex)
        assertTrue(sysexPackets.size >= 2)
        val realtime = UsbMidiPacketCodec.encode(b(0xF8))[0]  // MIDI clock

        val decoder = UsbMidiPacketCodec.Decoder()
        val decoded = ArrayList<ByteArray>()
        decoded.addAll(decoder.feed(sysexPackets[0]))
        decoded.addAll(decoder.feed(realtime))                 // interleaved
        for (p in sysexPackets.drop(1)) decoded.addAll(decoder.feed(p))

        assertEquals(2, decoded.size)
        assertArrayEquals(b(0xF8), decoded[0])   // the clock byte, standalone
        assertArrayEquals(sysex, decoded[1])     // the sysex, intact
    }

    @Test
    fun `multiple complete messages in one feed() call are all returned in order`() {
        val noteOn = UsbMidiPacketCodec.encode(b(0x90, 0x40, 0x7F))[0]
        val noteOff = UsbMidiPacketCodec.encode(b(0x80, 0x40, 0x00))[0]
        val cc = UsbMidiPacketCodec.encode(b(0xB0, 0x07, 0x64))[0]
        val transfer = noteOn + noteOff + cc

        val decoded = UsbMidiPacketCodec.Decoder().feed(transfer)
        assertEquals(3, decoded.size)
        assertArrayEquals(b(0x90, 0x40, 0x7F), decoded[0])
        assertArrayEquals(b(0x80, 0x40, 0x00), decoded[1])
        assertArrayEquals(b(0xB0, 0x07, 0x64), decoded[2])
    }

    @Test
    fun `an empty feed returns no messages, does not throw`() {
        assertTrue(UsbMidiPacketCodec.Decoder().feed(ByteArray(0)).isEmpty())
    }

    @Test
    fun `a packet split across transfer boundaries is retained and decoded once complete`() {
        val noteOn = UsbMidiPacketCodec.encode(b(0x90, 0x40, 0x7F))[0]
        val noteOff = UsbMidiPacketCodec.encode(b(0x80, 0x40, 0x00))[0]
        val decoder = UsbMidiPacketCodec.Decoder()

        val first = decoder.feed(noteOn + noteOff.copyOfRange(0, 2))
        assertEquals(1, first.size)
        assertArrayEquals(b(0x90, 0x40, 0x7F), first[0])

        val second = decoder.feed(noteOff.copyOfRange(2, 4))
        assertEquals(1, second.size)
        assertArrayEquals(b(0x80, 0x40, 0x00), second[0])
    }

    @Test
    fun `all one two and three byte packet splits are lossless`() {
        val packet = UsbMidiPacketCodec.encode(b(0xB0, 0x07, 0x64))[0]
        for (split in 1..3) {
            val decoder = UsbMidiPacketCodec.Decoder()
            assertTrue(decoder.feed(packet.copyOfRange(0, split)).isEmpty())
            val decoded = decoder.feed(packet.copyOfRange(split, 4))
            assertEquals("split=$split", 1, decoded.size)
            assertArrayEquals("split=$split", b(0xB0, 0x07, 0x64), decoded[0])
        }
    }

    @Test
    fun `unterminated sysex is bounded and discarded until its terminator`() {
        val decoder = UsbMidiPacketCodec.Decoder(maxSysexBytes = 12)
        val continuation = b(0x04, 0xF0, 0x01, 0x02)
        repeat(1000) { assertTrue(decoder.feed(continuation).isEmpty()) }

        // Completing an overflowed frame must not expose a truncated suffix.
        assertTrue(decoder.feed(b(0x05, 0xF7, 0x00, 0x00)).isEmpty())

        // The decoder recovers at the next independent, valid message.
        val noteOn = UsbMidiPacketCodec.encode(b(0x90, 0x40, 0x7F))[0]
        val decoded = decoder.feed(noteOn)
        assertEquals(1, decoded.size)
        assertArrayEquals(b(0x90, 0x40, 0x7F), decoded[0])
    }

    @Test
    fun `reserved CIN 0 and 1 packets are dropped silently, does not throw`() {
        val reserved0 = b(0x00, 0x11, 0x22, 0x33)
        val reserved1 = b(0x01, 0x11, 0x22, 0x33)
        val noteOn = UsbMidiPacketCodec.encode(b(0x90, 0x40, 0x7F))[0]
        val decoded = UsbMidiPacketCodec.Decoder().feed(reserved0 + reserved1 + noteOn)
        assertEquals(1, decoded.size)
        assertArrayEquals(b(0x90, 0x40, 0x7F), decoded[0])
    }

    @Test
    fun `a stray sysex-terminator CIN with nothing buffered still flushes without throwing`() {
        // Decoder started listening mid-stream (no preceding CIN 4 seen) -
        // this can only happen from a desynced/non-compliant sender, but
        // must never crash.
        val stray = b(0x06, 0x01, 0xF7, 0x00)
        val decoded = UsbMidiPacketCodec.Decoder().feed(stray)
        assertEquals(1, decoded.size)
        assertArrayEquals(b(0x01, 0xF7), decoded[0])
    }

    @Test
    fun `two independent sysex messages back to back both decode intact`() {
        val a = b(0xF0, 0x10, 0x11, 0xF7)
        val z = b(0xF0, 0x20, 0x21, 0x22, 0x23, 0xF7)
        val packets = UsbMidiPacketCodec.encode(a) + UsbMidiPacketCodec.encode(z)
        val decoder = UsbMidiPacketCodec.Decoder()
        val decoded = ArrayList<ByteArray>()
        for (p in packets) decoded.addAll(decoder.feed(p))
        assertEquals(2, decoded.size)
        assertArrayEquals(a, decoded[0])
        assertArrayEquals(z, decoded[1])
    }
}
