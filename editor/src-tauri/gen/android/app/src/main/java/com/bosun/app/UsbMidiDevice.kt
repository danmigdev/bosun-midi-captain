package com.bosun.app

import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.util.Log

/**
 * One USB-MIDI class-compliant device, talked to directly over
 * UsbDeviceConnection.bulkTransfer() on its MIDIStreaming interface -
 * bypassing android.media.midi entirely. See UsbMidiPacketCodec's class
 * doc for why: that framework's own USB-MIDI implementation has a
 * well-documented, longstanding bug where a port's receive callback
 * silently stops firing (no exception, no error) after an initial burst,
 * observed live on this project 2026-08-15 and independently reported by
 * other Android MIDI apps with no fix from the framework owners.
 *
 * The interface/endpoint discovery below follows the USB Device Class
 * Definition for MIDI Devices, Release 1.0: a MIDIStreaming interface is
 * bInterfaceClass=0x01 (AUDIO), bInterfaceSubClass=0x03 (MIDISTREAMING),
 * with one bulk IN and/or one bulk OUT endpoint carrying 4-byte USB-MIDI
 * Event Packets (see UsbMidiPacketCodec).
 */
class UsbMidiDevice private constructor(
    private val connection: UsbDeviceConnection,
    private val iface: UsbInterface,
    private val inEndpoint: UsbEndpoint?,
    private val outEndpoint: UsbEndpoint?,
    val label: String,
) {
    companion object {
        private const val TAG = "UsbMidiDevice"
        private const val USB_CLASS_AUDIO = 1
        private const val USB_SUBCLASS_MIDISTREAMING = 3

        /** Bulk read poll timeout. Short so the read loop notices `close()`
         * (via the `running` flag) promptly instead of blocking for a long
         * time with nothing to read - a timeout here is the normal idle
         * case, not an error. */
        private const val READ_TIMEOUT_MS = 250

        private const val WRITE_TIMEOUT_MS = 1000

        /** Bulk read buffer size: a multiple of 4 (the USB-MIDI packet
         * size) generous enough for a real burst (a Kemper rig-change
         * broadcast: several block-state CCs plus a rig-name SysEx) without
         * needing more than one bulkTransfer call to drain it. */
        private const val READ_BUF_SIZE = 1024

        /**
         * Finds the device's MIDIStreaming interface and bulk endpoints,
         * opens the device, and claims the interface. Returns null (closing
         * anything partially opened) on any failure: no USB permission, no
         * MIDIStreaming interface, no bulk endpoints, or a claim failure
         * (the interface is already claimed by someone else - e.g. if
         * android.media.midi's own service got to it first; callers should
         * make sure nothing else in the app opens this device via
         * MidiManager when using this path).
         */
        fun open(manager: UsbManager, device: UsbDevice, label: String): UsbMidiDevice? {
            if (!manager.hasPermission(device)) {
                Log.e(TAG, "[$label] no USB permission")
                return null
            }
            var midiIface: UsbInterface? = null
            for (i in 0 until device.interfaceCount) {
                val candidate = device.getInterface(i)
                if (candidate.interfaceClass == USB_CLASS_AUDIO &&
                    candidate.interfaceSubclass == USB_SUBCLASS_MIDISTREAMING
                ) {
                    midiIface = candidate
                    break
                }
            }
            if (midiIface == null) {
                Log.e(TAG, "[$label] no MIDIStreaming interface (class=1 subclass=3) found " +
                    "among ${device.interfaceCount} interface(s)")
                return null
            }
            var inEp: UsbEndpoint? = null
            var outEp: UsbEndpoint? = null
            for (i in 0 until midiIface.endpointCount) {
                val ep = midiIface.getEndpoint(i)
                if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
                if (ep.direction == UsbConstants.USB_DIR_IN) inEp = ep
                else if (ep.direction == UsbConstants.USB_DIR_OUT) outEp = ep
            }
            if (inEp == null && outEp == null) {
                Log.e(TAG, "[$label] MIDIStreaming interface has no bulk endpoints")
                return null
            }
            val connection = manager.openDevice(device)
            if (connection == null) {
                Log.e(TAG, "[$label] UsbManager.openDevice failed")
                return null
            }
            if (!connection.claimInterface(midiIface, true)) {
                Log.e(TAG, "[$label] claimInterface failed (already claimed elsewhere?)")
                runCatching { connection.close() }
                return null
            }
            Log.i(TAG, "[$label] opened: in=${inEp != null} out=${outEp != null}")
            return UsbMidiDevice(connection, midiIface, inEp, outEp, label)
        }
    }

    @Volatile private var running = false
    private var readThread: Thread? = null
    private val decoder = UsbMidiPacketCodec.Decoder()

    /**
     * Starts the background bulk-read loop, if there is an IN endpoint and
     * it is not already running. `onMessage` is invoked, on the reader
     * thread, for every complete MIDI message decoded from the stream -
     * callers must be thread-safe (this bridge's usage only ever touches
     * volatile timestamps and the other device's send(), both safe from any
     * thread).
     *
     * The loop itself never exits on a transient error: a bulkTransfer
     * failure or a decode exception is logged and the loop continues,
     * because this whole class exists to be MORE resilient than the
     * framework layer it replaces, not to introduce a new way to go silent.
     * It only stops when close() flips `running` false.
     */
    fun startReading(onMessage: (ByteArray) -> Unit) {
        val ep = inEndpoint ?: return
        if (running) return
        running = true
        val thread = Thread({
            val buf = ByteArray(READ_BUF_SIZE)
            while (running) {
                val n = try {
                    connection.bulkTransfer(ep, buf, buf.size, READ_TIMEOUT_MS)
                } catch (e: Exception) {
                    Log.e(TAG, "[$label] bulkTransfer read threw", e)
                    -1
                }
                if (n > 0) {
                    val messages = try {
                        decoder.feed(buf.copyOf(n))
                    } catch (e: Exception) {
                        Log.e(TAG, "[$label] decode crashed on ${n}B - dropping this chunk", e)
                        emptyList()
                    }
                    for (m in messages) {
                        try {
                            onMessage(m)
                        } catch (e: Exception) {
                            Log.e(TAG, "[$label] onMessage callback crashed", e)
                        }
                    }
                }
                // n == 0 or negative: normal read timeout (idle) or a
                // transient I/O hiccup. READ_TIMEOUT_MS already paces the
                // loop - no extra sleep, and no reason to treat either as
                // fatal or to stop the loop.
            }
        }, "UsbMidiDevice-read-$label")
        thread.isDaemon = true
        readThread = thread
        thread.start()
    }

    /**
     * Encodes and writes one complete MIDI message. Safe to call from any
     * thread; each call is one or more small, bounded bulkTransfer writes.
     * A write failure is logged and swallowed - matches the "a missed CC
     * beats a stuck pedal" philosophy already used on the firmware side's
     * own USB-MIDI retry loop (see midi.py's _tx_usb).
     */
    @Synchronized
    fun send(message: ByteArray): Boolean {
        val ep = outEndpoint ?: return false
        for (packet in UsbMidiPacketCodec.encode(message)) {
            val written = try {
                connection.bulkTransfer(ep, packet, packet.size, WRITE_TIMEOUT_MS)
            } catch (e: Exception) {
                Log.e(TAG, "[$label] bulkTransfer write threw", e)
                return false
            }
            if (written != packet.size) {
                Log.e(TAG, "[$label] short bulkTransfer write: $written/${packet.size}")
                return false
            }
        }
        return true
    }

    /** Stops the read loop and releases the interface/connection. Safe to
     * call more than once. */
    fun close() {
        running = false
        readThread?.let { t ->
            try {
                t.join(500)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
        readThread = null
        runCatching { connection.releaseInterface(iface) }
        runCatching { connection.close() }
    }
}
