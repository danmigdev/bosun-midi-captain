package com.bosun.app

/**
 * Pure USB-MIDI 1.0 Event Packet codec - no Android dependency, so it is
 * exercised entirely by local JVM unit tests (UsbMidiPacketCodecTest), no
 * emulator or device needed. Same design principle as BridgeHealthPolicy:
 * keep the part that must be RIGHT free of anything untestable.
 *
 * Every USB-MIDI packet is 4 bytes: a header byte (high nibble = Cable
 * Number, low nibble = Code Index Number / CIN) followed by up to 3 raw
 * MIDI data bytes, padded with zero where the CIN implies fewer. This is
 * the wire format on the bulk endpoints of a USB-MIDI class-compliant
 * device (both the Kemper and the Captain, via CircuitPython's usb_midi) -
 * a different, lower layer than android.media.midi's Java API.
 *
 * Why this exists instead of android.media.midi: that framework's own
 * USB-MIDI implementation has a well-documented, longstanding bug where a
 * port's MidiReceiver.onSend callback silently stops being invoked (no
 * exception, no error) after relaying an initial burst - observed live on
 * this project 2026-08-15 (Kemper->Captain direction: 3 messages relayed
 * then permanent silence for the rest of the session) and independently
 * reported against Android itself by other projects (android-midisuite
 * issue #60; a JUCE engineer who traced it to Android's ALSA-backed host
 * implementation and got no fix from the framework owners). This codec is
 * the wire-format half of talking to the USB bulk endpoints directly
 * (UsbMidiDevice does the Android/UsbDeviceConnection half), bypassing that
 * framework layer entirely rather than trying to detect-and-restart around it.
 *
 * Spec reference: Universal Serial Bus Device Class Definition for MIDI
 * Devices, Release 1.0, section 4, Table 4-1 "Code Index Number
 * Classifications". CIN-to-length table cross-checked against the
 * widely-used felis/USB_Host_Shield_2.0 Arduino USB-MIDI host driver.
 */
object UsbMidiPacketCodec {

    /** CIN (0x0-0xF, the packet header's low nibble) -> number of valid
     * data bytes among MIDI_0/1/2. */
    private val CIN_LENGTH = intArrayOf(0, 0, 2, 3, 3, 1, 2, 3, 3, 3, 3, 3, 2, 2, 3, 1)

    /** CIN for each System Common / Real-Time status 0xF0-0xFF, indexed by
     * (status and 0x0F). Index 0 (status 0xF0, SysEx start) is never read -
     * encode() special-cases SysEx via encodeSysEx() before consulting this
     * table, since a SysEx's CIN depends on how many bytes are left when it
     * ENDS, not on the F0 byte itself. */
    private val SYSTEM_CIN = intArrayOf(
        /* F0 */ 0x4,
        /* F1 */ 0x2, // MTC quarter frame
        /* F2 */ 0x3, // song position pointer
        /* F3 */ 0x2, // song select
        /* F4 */ 0x5, // undefined
        /* F5 */ 0x5, // undefined
        /* F6 */ 0x5, // tune request
        /* F7 */ 0x5, // end of sysex as a bare status (shouldn't reach here standalone)
        /* F8 */ 0xF, // timing clock
        /* F9 */ 0xF, // undefined
        /* FA */ 0xF, // start
        /* FB */ 0xF, // continue
        /* FC */ 0xF, // stop
        /* FD */ 0xF, // undefined
        /* FE */ 0xF, // active sensing
        /* FF */ 0xF, // system reset
    )

    /**
     * Encode ONE complete, already-framed MIDI message - a channel voice
     * message, a system common/real-time message, or a full 0xF0..0xF7
     * SysEx - into one or more 4-byte USB-MIDI packets. `cable` is the
     * Cable Number (0-15); this bridge only ever uses cable 0.
     *
     * A malformed message (empty, or a status byte with fewer data bytes
     * present than its type implies) never throws: empty input yields no
     * packets, and a short message is zero-padded like real hardware would
     * send. A bare data byte (status < 0x80, no status at all) also yields
     * no packets - there is nothing valid to frame.
     */
    fun encode(message: ByteArray, cable: Int = 0): List<ByteArray> {
        if (message.isEmpty()) return emptyList()
        val status = message[0].toInt() and 0xFF
        if (status < 0x80) return emptyList()
        if (status == 0xF0) return encodeSysEx(message, cable)

        val cin = if (status <= 0xEF) (status shr 4) else SYSTEM_CIN[status and 0x0F]
        val len = CIN_LENGTH[cin]
        val packet = ByteArray(4)
        packet[0] = (((cable and 0x0F) shl 4) or cin).toByte()
        for (i in 0 until len) {
            packet[i + 1] = if (i < message.size) message[i] else 0
        }
        return listOf(packet)
    }

    private fun encodeSysEx(message: ByteArray, cable: Int): List<ByteArray> {
        // Splits into 3-byte groups using CIN 0x4 ("starts or continues")
        // for every full group, then a final packet whose CIN (0x5/0x6/0x7)
        // encodes how many of the 1-3 remaining bytes are valid. This
        // terminating CIN just marks "last packet of this message" - it
        // does not require the last byte to literally be 0xF7, so a
        // message missing its trailing 0xF7 (truncated upstream) still
        // gets framed and round-trips through Decoder exactly as supplied,
        // rather than this codec silently dropping or altering bytes.
        val packets = ArrayList<ByteArray>()
        var i = 0
        val header = (cable and 0x0F) shl 4
        while (message.size - i > 3) {
            packets.add(byteArrayOf(
                (header or 0x4).toByte(),
                message[i], message[i + 1], message[i + 2],
            ))
            i += 3
        }
        val remaining = message.size - i
        val cin = when (remaining) {
            1 -> 0x5
            2 -> 0x6
            else -> 0x7 // remaining == 3 (remaining == 0 can't happen: F0 alone is remaining 1)
        }
        val packet = ByteArray(4)
        packet[0] = (header or cin).toByte()
        for (j in 0 until remaining) packet[j + 1] = message[i + j]
        packets.add(packet)
        return packets
    }

    /**
     * Stateful decoder for one direction of one cable's traffic. Feed raw
     * bytes as they arrive from bulk IN transfers - a transfer may contain
     * any number of whole 4-byte packets, and a SysEx spanning multiple
     * packets may itself span multiple feed() calls if a transfer boundary
     * falls in the middle of it. Returns every complete MIDI message
     * decoded from this call's bytes, in the order received.
     *
     * Never throws: a transfer whose length isn't a multiple of 4 (only
     * possible from a non-compliant device - real USB-MIDI bulk transfers
     * are always whole packets) has its trailing partial packet dropped,
     * and a SysEx-continuation CIN with no message in progress is treated
     * as the start of a new one rather than crashing on unexpected state.
     */
    class Decoder(
        private val maxSysexBytes: Int = DEFAULT_MAX_SYSEX_BYTES,
    ) {
        init {
            require(maxSysexBytes > 0) { "maxSysexBytes must be positive" }
        }

        companion object {
            /** Large enough for Kemper dumps, bounded to protect a long-lived bridge. */
            const val DEFAULT_MAX_SYSEX_BYTES = 64 * 1024
        }

        private val sysexBuf = ArrayList<Byte>()
        private var partialPacket = ByteArray(0)
        private var discardingOversizeSysex = false

        fun feed(data: ByteArray): List<ByteArray> {
            val out = ArrayList<ByteArray>()
            val framed = if (partialPacket.isEmpty()) data else partialPacket + data
            var i = 0
            while (i + 4 <= framed.size) {
                decodeOne(framed[i], framed[i + 1], framed[i + 2], framed[i + 3], out)
                i += 4
            }
            partialPacket = framed.copyOfRange(i, framed.size)
            return out
        }

        private fun decodeOne(b0: Byte, b1: Byte, b2: Byte, b3: Byte, out: MutableList<ByteArray>) {
            val cin = b0.toInt() and 0x0F
            when (cin) {
                0x0, 0x1 -> {
                    // Reserved/undefined per spec - a compliant device never
                    // sends these; drop silently rather than guess.
                }
                0x4 -> {
                    appendSysex(b1, b2, b3)
                }
                0x5 -> {
                    if ((b1.toInt() and 0xFF) == 0xF7) {
                        if (discardingOversizeSysex) resetSysex()
                        else {
                            appendSysex(b1)
                            if (!discardingOversizeSysex) out.add(flushSysex())
                            else resetSysex()
                        }
                    } else {
                        // Genuine single-byte System Common (undefined
                        // 0xF4/0xF5, or Tune Request 0xF6) - unrelated to
                        // any SysEx in progress, which (if any) is left
                        // untouched in sysexBuf.
                        out.add(byteArrayOf(b1))
                    }
                }
                0x6 -> {
                    finishSysex(out, b1, b2)
                }
                0x7 -> {
                    finishSysex(out, b1, b2, b3)
                }
                0xF -> {
                    // Single-byte Real-Time message. These legitimately
                    // interleave with an in-progress SysEx on the wire (the
                    // sending device packetizes them into their own CIN
                    // 0xF packet without disturbing the surrounding CIN 0x4
                    // continuation packets) - sysexBuf is untouched here.
                    out.add(byteArrayOf(b1))
                }
                else -> {
                    // 0x2/0x3 (System Common) and 0x8-0xE (channel voice):
                    // fixed length from the table, MIDI_0 is always the
                    // status byte.
                    val len = CIN_LENGTH[cin]
                    out.add(byteArrayOf(b1, b2, b3).copyOfRange(0, len))
                }
            }
        }

        private fun appendSysex(vararg bytes: Byte) {
            if (discardingOversizeSysex) return
            if (sysexBuf.size + bytes.size > maxSysexBytes) {
                sysexBuf.clear()
                discardingOversizeSysex = true
                return
            }
            bytes.forEach { sysexBuf.add(it) }
        }

        private fun finishSysex(out: MutableList<ByteArray>, vararg bytes: Byte) {
            if (discardingOversizeSysex) {
                resetSysex()
                return
            }
            appendSysex(*bytes)
            if (!discardingOversizeSysex) out.add(flushSysex()) else resetSysex()
        }

        private fun resetSysex() {
            sysexBuf.clear()
            discardingOversizeSysex = false
        }

        private fun flushSysex(): ByteArray {
            val result = sysexBuf.toByteArray()
            sysexBuf.clear()
            return result
        }
    }
}
