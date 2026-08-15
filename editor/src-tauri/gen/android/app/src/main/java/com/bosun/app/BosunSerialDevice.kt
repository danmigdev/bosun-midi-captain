package com.bosun.app

import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.util.Log

/**
 * One CDC-ACM "data" function on the Captain's composite USB device, talked
 * to directly over UsbDeviceConnection.bulkTransfer()/controlTransfer() -
 * bypassing tauri-plugin-serialplugin's Android backend entirely.
 *
 * Why: that backend (the android-usb-serial crate, via nusb's ioctl-based
 * transport) DOES set a per-call read/write timeout, but Android's own USB
 * host stack does not reliably honor it - a well-documented, long-standing
 * platform limitation (mik3y/usb-serial-for-android issue #159:
 * UsbDeviceConnection async requests can block indefinitely with no OS-level
 * timeout on API 17+). Confirmed live on this project 2026-08-15: "io read
 * hung"/"close hung" events roughly every 60-90s, each forcing a ~10-16s
 * stall+reconnect cycle - survivable (this project's own 8s call_with_timeout
 * watchdog in serial/android.rs already bounds it) but frequent enough to
 * feel broken. This is the exact same class of fix already applied to the
 * MIDI side tonight (see UsbMidiDevice.kt's doc comment): move off whatever
 * API doesn't honor its own timeout onto UsbDeviceConnection.bulkTransfer(),
 * Android's official SYNCHRONOUS transfer call with real SDK-level timeout
 * enforcement - a different, better-trodden code path.
 *
 * Interface discovery: the Captain's firmware calls
 * usb_cdc.enable(console=True, data=True) in boot.py, and CircuitPython
 * enumerates `console` first (lower interface numbers) and `data` second
 * (higher interface numbers) - matching the existing sort_ports_desc
 * heuristic in android_helpers.rs ("data CDC index 1+ tried before console
 * CDC index 0"). Each CDC-ACM function is a Communications interface
 * (class 0x02) immediately followed by its paired Data interface (class
 * 0x0A) per the USB CDC spec's required Interface Association Descriptor
 * grouping. This class finds the CDC_DATA-class interface with the highest
 * interface number (= the "data" function, not console) and its paired
 * Comm interface (= that data interface's number minus one).
 */
class BosunSerialDevice private constructor(
    private val connection: UsbDeviceConnection,
    private val commIface: UsbInterface,
    private val dataIface: UsbInterface,
    private val inEndpoint: UsbEndpoint,
    private val outEndpoint: UsbEndpoint,
) {
    companion object {
        private const val TAG = "BosunSerialDevice"

        private const val USB_CLASS_COMM = 0x02
        private const val USB_CLASS_CDC_DATA = 0x0A

        // USB CDC 1.2 class-specific control requests (Communications
        // Device Class spec, section 6.2). Sent as a control transfer
        // targeting the Comm interface: requestType = USB_TYPE_CLASS |
        // USB_RECIP_INTERFACE | USB_DIR_OUT (host to device).
        private const val CDC_REQ_SET_LINE_CODING = 0x20
        private const val CDC_REQ_SET_CONTROL_LINE_STATE = 0x22
        private const val CDC_CTRL_DTR = 0x01
        private const val CDC_CTRL_RTS = 0x02
        private const val CDC_CONTROL_REQUEST_TYPE =
            UsbConstants.USB_TYPE_CLASS or UsbConstants.USB_DIR_OUT or 0x01 // recipient = interface

        private const val CONTROL_TIMEOUT_MS = 1000

        /**
         * Finds the data CDC-ACM function, opens the device, and claims
         * both its interfaces. Returns null (releasing anything partially
         * claimed) on any failure: no USB permission, fewer than two
         * CDC_DATA interfaces found (console's + data's), no bulk
         * endpoints, or a claim failure (already claimed elsewhere - the
         * caller must make sure tauri-plugin-serialplugin's own connection
         * to this port is closed first).
         */
        fun open(manager: UsbManager, device: UsbDevice): BosunSerialDevice? {
            if (!manager.hasPermission(device)) {
                Log.e(TAG, "no USB permission")
                return null
            }
            val allIfaces = (0 until device.interfaceCount).map { device.getInterface(it) }
            val dataCandidates = allIfaces
                .filter { it.interfaceClass == USB_CLASS_CDC_DATA }
                .sortedBy { it.id }
            if (dataCandidates.size < 2) {
                Log.e(
                    TAG,
                    "expected 2+ CDC_DATA interfaces (console + data), found " +
                        "${dataCandidates.size} among ${device.interfaceCount} interface(s)"
                )
                return null
            }
            // Highest interface number = enabled second in boot.py = "data".
            val dataIface = dataCandidates.last()
            val commIface = allIfaces.firstOrNull {
                it.interfaceClass == USB_CLASS_COMM && it.id == dataIface.id - 1
            }
            if (commIface == null) {
                Log.e(TAG, "no paired Comm interface (id=${dataIface.id - 1}) for data interface id=${dataIface.id}")
                return null
            }
            var inEp: UsbEndpoint? = null
            var outEp: UsbEndpoint? = null
            for (i in 0 until dataIface.endpointCount) {
                val ep = dataIface.getEndpoint(i)
                if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
                if (ep.direction == UsbConstants.USB_DIR_IN) inEp = ep else outEp = ep
            }
            if (inEp == null || outEp == null) {
                Log.e(TAG, "data interface (id=${dataIface.id}) missing a bulk IN or OUT endpoint")
                return null
            }
            val connection = manager.openDevice(device)
            if (connection == null) {
                Log.e(TAG, "UsbManager.openDevice failed")
                return null
            }
            if (!connection.claimInterface(commIface, true)) {
                Log.e(TAG, "claimInterface(comm id=${commIface.id}) failed")
                runCatching { connection.close() }
                return null
            }
            if (!connection.claimInterface(dataIface, true)) {
                Log.e(TAG, "claimInterface(data id=${dataIface.id}) failed")
                runCatching { connection.releaseInterface(commIface) }
                runCatching { connection.close() }
                return null
            }
            Log.i(TAG, "opened: comm iface=${commIface.id} data iface=${dataIface.id}")
            val dev = BosunSerialDevice(connection, commIface, dataIface, inEp, outEp)
            // Match the desktop/plugin behavior every part of this codebase
            // already assumes: DTR asserted on open triggers the RP2040's
            // CP soft-reset, and 115200 8N1 line coding (CircuitPython's
            // CDC ACM ignores the actual baud value but some hosts/drivers
            // expect a well-formed SET_LINE_CODING regardless).
            dev.setLineCoding(115200)
            dev.setDtr(true)
            return dev
        }
    }

    /** CDC SET_LINE_CODING: 7 bytes, little-endian baud rate + 1 stop bit +
     * no parity + 8 data bits. CircuitPython's CDC ACM does not act on this
     * (it is not a real UART), but sending a well-formed request matches
     * what every other host driver does and avoids surprising the RP2040's
     * TinyUSB CDC class implementation with an unexpected wLength. */
    private fun setLineCoding(baud: Int): Boolean {
        val data = byteArrayOf(
            (baud and 0xFF).toByte(),
            ((baud shr 8) and 0xFF).toByte(),
            ((baud shr 16) and 0xFF).toByte(),
            ((baud shr 24) and 0xFF).toByte(),
            0, // bCharFormat: 1 stop bit
            0, // bParityType: none
            8, // bDataBits
        )
        val n = connection.controlTransfer(
            CDC_CONTROL_REQUEST_TYPE, CDC_REQ_SET_LINE_CODING, 0, commIface.id,
            data, data.size, CONTROL_TIMEOUT_MS
        )
        if (n < 0) Log.w(TAG, "SET_LINE_CODING failed (n=$n)")
        return n >= 0
    }

    /** CDC SET_CONTROL_LINE_STATE: bit 0 = DTR, bit 1 = RTS. Both asserted
     * together to match how every other client in this codebase (the
     * desktop serial2 path, the old plugin-based Android path) opens the
     * port - the RP2040 resets on the DTR edge either way, and CircuitPython
     * does not distinguish DTR from RTS for its own "connected" state. */
    fun setDtr(on: Boolean): Boolean {
        val value = if (on) (CDC_CTRL_DTR or CDC_CTRL_RTS) else 0
        val n = connection.controlTransfer(
            CDC_CONTROL_REQUEST_TYPE, CDC_REQ_SET_CONTROL_LINE_STATE, value, commIface.id,
            null, 0, CONTROL_TIMEOUT_MS
        )
        if (n < 0) Log.w(TAG, "SET_CONTROL_LINE_STATE($on) failed (n=$n)")
        return n >= 0
    }

    /**
     * Reads up to `buf.size` bytes, blocking at most `timeoutMs`. Returns
     * the byte count read, or 0 if nothing arrived within the timeout.
     *
     * UsbDeviceConnection.bulkTransfer() returns -1 for BOTH a genuine I/O
     * error and a plain "nothing available within the timeout" - Android
     * gives callers no way to tell the two apart from the return value
     * alone (confirmed live, 2026-08-15: treating every negative read as a
     * fatal error turned normal idle polling into a permanent
     * stall-recovery loop, reconnecting every ~1.5 s). UsbMidiDevice's own
     * read loop already made this same call for the MIDI side ("n == 0 or
     * negative: normal read timeout... no reason to treat either as
     * fatal") - matched here. A link that is genuinely dead still gets
     * caught by the wall-clock stall detection in android.rs, same as
     * before this class existed.
     */
    fun read(buf: ByteArray, timeoutMs: Int): Int {
        val n = connection.bulkTransfer(inEndpoint, buf, buf.size, timeoutMs)
        return if (n > 0) n else 0
    }

    /** Writes all of `data`, retrying internally on a partial transfer
     * (bulkTransfer can legitimately return fewer bytes than requested,
     * e.g. if a timeout lands mid-transfer) until every byte is sent or the
     * overall `timeoutMs` budget runs out. Returns the total byte count
     * written - equal to data.size on full success, less if the deadline
     * ran out first (BosunSerialBridge.write() turns that into a "timeout"
     * exception so android.rs's is_transient_error() retries it rather
     * than treating a partial send as either full success or a fatal
     * error). A negative bulkTransfer result is treated the same as a
     * plain 0 - see read()'s doc comment for why: Android does not
     * distinguish a timeout from a real error here either. */
    fun write(data: ByteArray, timeoutMs: Int): Int {
        var sent = 0
        val deadline = System.nanoTime() + timeoutMs.toLong() * 1_000_000L
        while (sent < data.size) {
            val remainingMs = ((deadline - System.nanoTime()) / 1_000_000L).toInt()
            if (remainingMs <= 0) break
            val chunk = data.copyOfRange(sent, data.size)
            val n = connection.bulkTransfer(outEndpoint, chunk, chunk.size, remainingMs)
            if (n <= 0) break // timed out (or ambiguous error) with nothing sent this round
            sent += n
        }
        return sent
    }

    /** Releases both interfaces and closes the connection. Safe to call
     * more than once. */
    fun close() {
        runCatching { connection.releaseInterface(dataIface) }
        runCatching { connection.releaseInterface(commIface) }
        runCatching { connection.close() }
    }
}
