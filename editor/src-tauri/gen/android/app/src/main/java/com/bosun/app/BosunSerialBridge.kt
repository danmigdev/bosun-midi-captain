package com.bosun.app

import android.content.Context
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.util.Log

/**
 * JNI-facing singleton owning the raw-USB data-CDC connection to the
 * Captain (see BosunSerialDevice's doc comment for why this bypasses
 * tauri-plugin-serialplugin's Android backend). One logical port,
 * "usb-data" - there is only ever one Captain to talk to on this
 * connection, unlike the desktop path's real multi-port COM enumeration.
 *
 * Lifecycle is driven from Rust through JNI (see serial_android_native.rs),
 * same shape as BosunMidiBridge.kt:
 *
 * ```kotlin
 * object BosunSerialBridge {
 *   @JvmStatic fun listPorts(context: Context): Array<String>
 *   @JvmStatic fun open(context: Context, port: String): String   // throws on failure
 *   @JvmStatic fun close(context: Context)
 *   @JvmStatic fun read(maxLen: Int, timeoutMs: Int): ByteArray   // empty = timeout, never null
 *   @JvmStatic fun write(data: ByteArray, timeoutMs: Int): Int    // bytes written, or -1
 * }
 * ```
 */
object BosunSerialBridge {
    private const val TAG = "BosunSerialBridge"

    /** The single synthetic port name this bridge ever reports/accepts. */
    const val PORT_NAME = "usb-data"

    /** Same vendor IDs BosunMidiBridge already matches the Captain against
     * (Adafruit/CircuitPython + Raspberry Pi RP2040 boards). */
    private val CAPTAIN_VENDOR_IDS = setOf(0x239A, 0x2E8A)

    @Volatile private var device: BosunSerialDevice? = null

    @JvmStatic
    @Synchronized
    fun listPorts(context: Context): Array<String> {
        val manager = usbManager(context) ?: return emptyArray()
        val found = manager.deviceList.values.any { matchesCaptain(it) }
        return if (found) arrayOf(PORT_NAME) else emptyArray()
    }

    /** Opens (or reopens - closes any existing connection first) the
     * Captain's data CDC interface. Returns the canonical port name on
     * success; throws with a descriptive message on failure so the JNI
     * caller can surface it the same way the old plugin's Result<String,
     * String> did. */
    @JvmStatic
    @Synchronized
    fun open(context: Context, @Suppress("UNUSED_PARAMETER") port: String): String {
        close(context)
        val manager = usbManager(context)
            ?: throw IllegalStateException("UsbManager service not available")
        val target = manager.deviceList.values.firstOrNull { matchesCaptain(it) }
            ?: throw IllegalStateException("Captain USB device not found")
        val opened = BosunSerialDevice.open(manager, target)
            ?: throw IllegalStateException("failed to open/claim the Captain's data CDC interface (see logcat)")
        device = opened
        Log.i(TAG, "opened $PORT_NAME")
        return PORT_NAME
    }

    @JvmStatic
    @Synchronized
    fun close(@Suppress("UNUSED_PARAMETER") context: Context) {
        device?.close()
        device = null
    }

    /** Reads up to maxLen bytes, blocking at most timeoutMs. Returns an
     * empty array whenever nothing arrived within the timeout - never
     * throws for that case, since BosunSerialDevice.read() already
     * collapses bulkTransfer's ambiguous negative-or-timeout result down
     * to a plain 0 (see its doc comment for why). Only "not connected"
     * throws here. */
    @JvmStatic
    fun read(maxLen: Int, timeoutMs: Int): ByteArray {
        val dev = device ?: throw IllegalStateException("not connected")
        val buf = ByteArray(maxLen)
        val n = dev.read(buf, timeoutMs)
        return if (n == 0) ByteArray(0) else buf.copyOf(n)
    }

    /** Writes data, blocking at most timeoutMs. Returns data.size on full
     * success. Throws with a message containing "timeout" on a partial or
     * zero send (android.rs's is_transient_error() matches that substring
     * and re-queues the command for retry, rather than treating a partial
     * send as either silent success or a fatal, connection-killing error). */
    @JvmStatic
    fun write(data: ByteArray, timeoutMs: Int): Int {
        val dev = device ?: throw IllegalStateException("not connected")
        val n = dev.write(data, timeoutMs)
        if (n < data.size) throw java.io.IOException("write timeout: sent $n/${data.size} bytes")
        return n
    }

    private fun usbManager(context: Context): UsbManager? =
        context.applicationContext.getSystemService(Context.USB_SERVICE) as? UsbManager

    /** Same matching approach as BosunMidiBridge.matches(): device label
     * (manufacturer + product name) contains a known pattern, falling back
     * to vendor-ID matching when string descriptors are unavailable. No
     * hint parameter here - there is only ever one Captain on this
     * connection, unlike the MIDI bridge's Kemper-vs-pedal disambiguation. */
    private fun matchesCaptain(usbDevice: UsbDevice): Boolean {
        val label = listOfNotNull(usbDevice.manufacturerName, usbDevice.productName)
            .joinToString(" ").trim()
        if (label.isNotBlank()) {
            return listOf("circuitpython", "captain", "pico", "raspberry")
                .any { label.contains(it, ignoreCase = true) }
        }
        return usbDevice.vendorId in CAPTAIN_VENDOR_IDS
    }
}
