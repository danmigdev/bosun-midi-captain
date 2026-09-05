package com.bosun.app

import android.content.Context
import android.content.Intent
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.util.Log
import androidx.core.content.ContextCompat

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
 *   @JvmStatic fun open(context: Context, port: String, generation: Long): String
 *   @JvmStatic fun close(context: Context, generation: Long)
 *   @JvmStatic fun read(generation: Long, maxLen: Int, timeoutMs: Int): ByteArray
 *   @JvmStatic fun write(generation: Long, data: ByteArray, timeoutMs: Int): Int
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
    private val generations = SessionGenerationFence()

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
    fun open(context: Context, @Suppress("UNUSED_PARAMETER") port: String, generation: Long): String {
        require(generation > 0) { "invalid session generation" }
        if (!generations.begin(generation)) throw IllegalStateException("stale session")
        closeCurrent(context)
        val manager = usbManager(context)
            ?: throw IllegalStateException("UsbManager service not available")
        val target = manager.deviceList.values.firstOrNull { matchesCaptain(it) }
            ?: throw IllegalStateException("Captain USB device not found")
        val opened = BosunSerialDevice.open(manager, target)
            ?: throw IllegalStateException("failed to open/claim the Captain's data CDC interface (see logcat)")
        try {
            ContextCompat.startForegroundService(
                context.applicationContext,
                Intent(context.applicationContext, BosunSerialService::class.java),
            )
            device = opened
            check(generations.activate(generation)) { "session superseded while opening" }
        } catch (e: Exception) {
            opened.close()
            throw IllegalStateException("failed to start serial foreground service", e)
        }
        Log.i(TAG, "opened $PORT_NAME")
        return PORT_NAME
    }

    @JvmStatic
    @Synchronized
    fun close(context: Context, generation: Long) {
        if (!generations.release(generation)) return
        closeCurrent(context)
    }

    private fun closeCurrent(context: Context) {
        device?.close()
        device = null
        context.applicationContext.stopService(
            Intent(context.applicationContext, BosunSerialService::class.java),
        )
    }

    /** Reads up to maxLen bytes, blocking at most timeoutMs. Returns an
     * empty array whenever nothing arrived within the timeout - never
     * throws for that case, since BosunSerialDevice.read() already
     * collapses bulkTransfer's ambiguous negative-or-timeout result down
     * to a plain 0 (see its doc comment for why). Only "not connected"
     * throws here. */
    @JvmStatic
    fun read(generation: Long, maxLen: Int, timeoutMs: Int): ByteArray {
        val dev = device?.takeIf { generations.owns(generation) }
            ?: throw IllegalStateException("stale or disconnected session")
        val buf = ByteArray(maxLen)
        val n = dev.read(buf, timeoutMs)
        if (!generations.owns(generation) || device !== dev) {
            throw IllegalStateException("stale session completed after reconnect")
        }
        return if (n == 0) ByteArray(0) else buf.copyOf(n)
    }

    /** Writes data, blocking at most timeoutMs. Returns data.size on full
     * success. Throws with a message containing "timeout" on a partial or
     * zero send (android.rs's is_transient_error() matches that substring
     * and re-queues the command for retry, rather than treating a partial
     * send as either silent success or a fatal, connection-killing error). */
    @JvmStatic
    fun write(generation: Long, data: ByteArray, timeoutMs: Int): Int {
        val dev = device?.takeIf { generations.owns(generation) }
            ?: throw IllegalStateException("stale or disconnected session")
        val n = dev.write(data, timeoutMs)
        if (!generations.owns(generation) || device !== dev) {
            throw IllegalStateException("stale session completed after reconnect")
        }
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
