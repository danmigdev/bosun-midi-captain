package com.bosun.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Bridges MIDI traffic between a Kemper device (Player or Profiler) and a
 * MIDI Captain pedal when both are plugged into the Android device through a
 * USB hub. Devices are discovered through UsbManager and talked to directly
 * over UsbDeviceConnection.bulkTransfer() on their MIDIStreaming interface
 * (see UsbMidiDevice) - NOT through android.media.midi.
 *
 * That framework was the original implementation and worked most of the
 * time, but its own USB-MIDI layer has a well-documented, longstanding bug:
 * a port's receive callback can silently stop firing (no exception, no
 * error - the reader thread just dies) after relaying an initial burst.
 * Observed live on this project 2026-08-15 (Kemper -> Captain: exactly 3
 * messages relayed, then permanent silence for the rest of the session
 * while Captain -> Kemper kept working) and independently reported by other
 * Android MIDI apps (android-midisuite issue #60; a JUCE engineer traced it
 * to Android's ALSA-backed USB host implementation and got no fix from the
 * framework owners). UsbMidiPacketCodec + UsbMidiDevice bypass that layer
 * entirely instead of trying to detect-and-restart around it - the same
 * raw-USB approach this app's own SerialPlugin already uses reliably for
 * the Captain's other USB interface (data CDC), which never showed this
 * failure mode.
 *
 * All MIDI messages are forwarded in both directions (Kemper IN -> Captain
 * OUT and Captain IN -> Kemper OUT) except MIDI clock (0xF8) and active
 * sensing (0xFE), which the pedal does not need.
 *
 * Lifecycle is driven from Rust through JNI. This is a singleton (object), so
 * the JNI class is com.bosun.app.BosunMidiBridge and the methods are static.
 * Kotlin contract - keep in sync with `midi_android.rs`:
 *
 * ```kotlin
 * object BosunMidiBridge {
 *   data class BridgeStatus(val active: Boolean, val kemperPort: String?, val pedalPort: String?)
 *   data class MidiPorts(val inputs: Array<String>, val outputs: Array<String>)
 *
 *   @JvmStatic fun listPorts(context: Context): MidiPorts
 *   @JvmStatic fun start(context: Context, kemper: String?, pedal: String?): BridgeStatus
 *   @JvmStatic fun stop(context: Context): BridgeStatus
 *   @JvmStatic fun status(context: Context): BridgeStatus
 * }
 * ```
 *
 * The kemper/captain hints narrow the device search; when a hint is blank the
 * standard name patterns are used ("profiler"/"kemper" for the Kemper,
 * "circuitpython"/"captain" for the pedal), with vendor-ID matching
 * (res/xml/device_filter.xml's set) as a fallback when USB string
 * descriptors are unavailable.
 */
object BosunMidiBridge {

    private const val TAG = "BosunMidiBridge"

    /** MIDI clock realtime message, noise for the pedal, always dropped. */
    private const val MIDI_CLOCK = 0xF8

    /** Active sensing realtime message, noise for the pedal, always dropped. */
    private const val MIDI_ACTIVE_SENSING = 0xFE

    /** How long to wait for the USB MIDI devices to appear after start. */
    private const val ENUMERATION_TIMEOUT_MS = 2500L

    /** Poll interval used while waiting for device enumeration. */
    private const val ENUMERATION_POLL_MS = 100L

    /** Kemper GmbH. */
    private val KEMPER_VENDOR_IDS = setOf(0x133E)

    /** Adafruit (CircuitPython) and Raspberry Pi (other RP2040 boards) -
     *  see res/xml/device_filter.xml and MainActivity.BOSUN_VENDOR_IDS. */
    private val CAPTAIN_VENDOR_IDS = setOf(0x239A, 0x2E8A)

    /**
     * Snapshot of the bridge state, returned to Rust from JNI.
     * kemperPort/pedalPort are the human-readable labels of the bridged
     * devices (or null when a device was not found/opened).
     */
    data class BridgeStatus(
        val active: Boolean,
        val kemperPort: String?,
        val pedalPort: String?
    )

    /** Enumerated MIDI-capable USB devices visible to the system. */
    data class MidiPorts(
        val inputs: Array<String>,
        val outputs: Array<String>
    )

    // State below is only touched from start()/stop() (both synchronized) or
    // read as volatile fields by status() and the detach receiver.

    private var bridgeThread: HandlerThread? = null
    private var bridgeHandler: Handler? = null

    /** App context saved at start(); used by the detach receiver's stop(). */
    @Volatile private var appContext: Context? = null

    private var kemperDevice: UsbMidiDevice? = null
    private var captainDevice: UsbMidiDevice? = null

    @Volatile private var active = false
    @Volatile private var kemperLabel: String? = null
    @Volatile private var captainLabel: String? = null

    /** SystemClock.elapsedRealtime() of the last message relayed in each
     *  direction, 0 = none yet this session. Per-direction because the
     *  bridge's own `active` flag only reflects whether start() succeeded -
     *  a direction can go silent without `active` ever flipping false. The
     *  self-check below is what actually notices that split-brain state. */
    @Volatile private var lastK2cMs: Long = 0
    @Volatile private var lastC2kMs: Long = 0
    @Volatile private var bridgeStartedAtMs: Long = 0
    /** SystemClock.elapsedRealtime() of the last self-heal restart this
     *  policy actually acted on, 0 = never. Feeds BridgeHealthPolicy's
     *  cooldown guard - see BridgeHealthPolicy.kt for why this exists
     *  (2026-08-15 regression: a restart every ~15 s, forever). */
    @Volatile private var lastHealthRestartAtMs: Long = 0
    /** Bumped every time scheduleHealthCheck() is (re)armed from start().
     *  Each posted Runnable captures the generation it was armed with and
     *  drops itself the moment it no longer matches. */
    @Volatile private var healthCheckGeneration: Int = 0

    /** How often the self-check evaluates BridgeHealthPolicy. Independent
     *  of the policy's own thresholds (STARTUP_GRACE_MS / STALE_AFTER_MS /
     *  MIN_RESTART_INTERVAL_MS, all in BridgeHealthPolicy.kt) - this is
     *  just the polling cadence. */
    private const val HEALTH_CHECK_POLL_MS = 15000L

    /** deviceId of the bridged UsbDevices, used to react when one is
     *  unplugged (compared against ACTION_USB_DEVICE_DETACHED's extra). */
    @Volatile private var kemperDeviceId = -1
    @Volatile private var captainDeviceId = -1

    private var detachReceiver: BroadcastReceiver? = null

    private fun makeDetachReceiver() = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != UsbManager.ACTION_USB_DEVICE_DETACHED) return
            val device = getUsbDeviceExtra(intent) ?: return
            if (device.deviceId == kemperDeviceId || device.deviceId == captainDeviceId) {
                Log.i(TAG, "Bridged USB device unplugged (${deviceLabel(device)}), stopping bridge")
                // Do not call stop() synchronously from inside a receiver
                // callback - tear down on a dedicated thread, matching the
                // old MidiManager.DeviceCallback's own precaution.
                val ctx = appContext
                Thread({
                    ctx?.let { runCatching { stop(it) } }
                }, "BosunMidiBridge-cleanup").start()
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun getUsbDeviceExtra(intent: Intent): UsbDevice? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
        } else {
            intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
        }

    /** Enumerate the MIDI-capable USB devices currently visible to the
     * system (i.e. anything the OS sees on the bus with USB permission
     * already granted - devices without permission can still be found by
     * VID/name but their exact port count isn't queryable). */
    @JvmStatic
    fun listPorts(context: Context): MidiPorts {
        val manager = context.applicationContext
            .getSystemService(Context.USB_SERVICE) as? UsbManager
            ?: return MidiPorts(emptyArray(), emptyArray())
        val inputs = ArrayList<String>()
        val outputs = ArrayList<String>()
        for (device in manager.deviceList.values) {
            val label = deviceLabel(device)
            inputs.add("$label IN")
            outputs.add("$label OUT")
        }
        return MidiPorts(inputs.toTypedArray(), outputs.toTypedArray())
    }

    /**
     * Opens the Kemper and the Captain, wires Kemper IN to Captain OUT and
     * Captain IN to Kemper OUT, and returns the resulting status. Blocks the
     * calling thread (the Rust JNI thread) until both devices are found or
     * the enumeration timeout expires, so the returned status is final.
     *
     * Idempotent: if the bridge is already active the current status is
     * returned unchanged.
     */
    @JvmStatic
    @Synchronized
    fun start(context: Context, kemperHint: String?, captainHint: String?): BridgeStatus {
        if (active) return status(context)

        appContext = context.applicationContext
        val manager = context.applicationContext
            .getSystemService(Context.USB_SERVICE) as? UsbManager
        if (manager == null) {
            Log.e(TAG, "UsbManager service not available")
            return BridgeStatus(false, null, null)
        }

        ensureBridgeThread()
        val handler = bridgeHandler ?: return BridgeStatus(false, null, null)

        val (kemperInfo, captainInfo) = findDevices(manager, kemperHint, captainHint)
        if (kemperInfo == null || captainInfo == null) {
            Log.e(
                TAG,
                "MIDI bridge needs both devices. Kemper: ${kemperInfo?.let { deviceLabel(it) } ?: "not found"}" +
                    " (hint: ${kemperHint ?: "none"}), " +
                    "Captain: ${captainInfo?.let { deviceLabel(it) } ?: "not found"}" +
                    " (hint: ${captainHint ?: "none"})"
            )
            return BridgeStatus(false, kemperInfo?.let { deviceLabel(it) }, captainInfo?.let { deviceLabel(it) })
        }

        val kemperLbl = deviceLabel(kemperInfo)
        val captainLbl = deviceLabel(captainInfo)
        val kemper = UsbMidiDevice.open(manager, kemperInfo, kemperLbl)
        val captain = UsbMidiDevice.open(manager, captainInfo, captainLbl)
        if (kemper == null || captain == null) {
            Log.e(TAG, "Failed to open the MIDI devices")
            kemper?.close()
            captain?.close()
            return BridgeStatus(false, kemperLbl, captainLbl)
        }
        kemperDevice = kemper
        captainDevice = captain

        // Kemper -> Captain, Captain -> Kemper. Each direction's onMessage
        // both stamps the liveness timestamp the health check watches AND
        // forwards (minus clock/active-sensing noise) into the other device.
        kemper.startReading { msg ->
            lastK2cMs = SystemClock.elapsedRealtime()
            if (!isNoise(msg)) captain.send(msg)
        }
        captain.startReading { msg ->
            lastC2kMs = SystemClock.elapsedRealtime()
            if (!isNoise(msg)) kemper.send(msg)
        }
        Log.i(TAG, "Bridge links: kemper->captain and captain->kemper both wired")

        kemperDeviceId = kemperInfo.deviceId
        captainDeviceId = captainInfo.deviceId
        val receiver = makeDetachReceiver()
        ContextCompat.registerReceiver(
            appContext!!, receiver, IntentFilter(UsbManager.ACTION_USB_DEVICE_DETACHED),
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
        detachReceiver = receiver

        active = true
        kemperLabel = kemperLbl
        captainLabel = captainLbl
        bridgeStartedAtMs = SystemClock.elapsedRealtime()
        // Reset per-direction liveness so a PREVIOUS session's stale
        // timestamps (from a direction that had already died) can't
        // immediately fail the first health check of this new session.
        lastK2cMs = 0
        lastC2kMs = 0
        Log.i(TAG, "Bridge active: $kemperLabel <-> $captainLabel")
        scheduleHealthCheck(handler, ++healthCheckGeneration)
        return status(context)
    }

    /** True for MIDI clock (0xF8) and active sensing (0xFE) - noise the
     * pedal does not need. Each decoded message from UsbMidiPacketCodec is
     * already a single complete message, so this is just a status check -
     * no byte-scanning needed (unlike the old android.media.midi path,
     * which received raw multi-message chunks and had to scan them). */
    private fun isNoise(message: ByteArray): Boolean {
        if (message.isEmpty()) return false
        val status = message[0].toInt() and 0xFF
        return status == MIDI_CLOCK || status == MIDI_ACTIVE_SENSING
    }

    /**
     * Periodic self-check: if EITHER direction has relayed nothing since
     * before BridgeHealthPolicy's startup grace period, that direction's
     * reader has effectively died even though `active` itself never flips
     * false - the coarse app-level health check (App.svelte polling
     * midi_bridge_status()) cannot see this split-brain state, only a
     * from-inside check that knows about both directions can.
     *
     * The actual grace/staleness/cooldown judgment lives in
     * BridgeHealthPolicy (unit-tested, see BridgeHealthPolicyTest) - this
     * function only supplies the live clock readings and acts on the
     * verdict. Detection only, matching the original android.media.midi
     * version's own hard-learned lesson (see the 2026-08-15 comment history
     * in BridgeHealthPolicy.kt): auto-restarting from here needs a real
     * design for "device permission may be needed again" and "don't
     * contend with the main thread for the object lock" - not attempted
     * here either. UsbMidiDevice's read loop is the actual fix for the
     * specific failure this was built to catch (a silently-dead reader);
     * this stays wired in as a second line of defense and a visible log
     * line if something else ever produces the same symptom.
     */
    private fun scheduleHealthCheck(handler: Handler, generation: Int) {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (!active || generation != healthCheckGeneration) return
                val now = SystemClock.elapsedRealtime()
                val verdict = BridgeHealthPolicy.evaluate(
                    nowMs = now,
                    bridgeStartedAtMs = bridgeStartedAtMs,
                    lastK2cMs = lastK2cMs,
                    lastC2kMs = lastC2kMs,
                    lastRestartAtMs = lastHealthRestartAtMs,
                )
                if (verdict.k2cStale || verdict.c2kStale) {
                    Log.w(
                        TAG,
                        "MIDI bridge looks half-dead (kemper->captain stale=${verdict.k2cStale}, " +
                            "captain->kemper stale=${verdict.c2kStale}) - NOT auto-restarting (see comment)"
                    )
                }
                handler.postDelayed(this, HEALTH_CHECK_POLL_MS)
            }
        }, HEALTH_CHECK_POLL_MS)
    }

    /** Closes every open device and marks the bridge inactive.
     * The Context parameter exists purely for the JNI signature parity with
     * listPorts/start - the bridge itself does not need it. */
    @JvmStatic
    @Synchronized
    fun stop(context: Context): BridgeStatus {
        if (active || kemperDevice != null || captainDevice != null) {
            Log.i(TAG, "Stopping MIDI bridge")
            // Clear the ids first so a queued detach broadcast cannot
            // re-trigger a stop while we are tearing down.
            kemperDeviceId = -1
            captainDeviceId = -1
            closeEverything()
            active = false
            kemperLabel = null
            captainLabel = null
        }
        return status(context)
    }

    /** Current bridge state; safe to call at any time from Rust. */
    @JvmStatic
    fun status(context: Context): BridgeStatus =
        BridgeStatus(active, kemperLabel, captainLabel)

    // ---- internals ----

    private fun ensureBridgeThread() {
        if (bridgeThread != null) return
        val thread = HandlerThread("BosunMidiBridge").apply { start() }
        bridgeThread = thread
        bridgeHandler = Handler(thread.looper)
    }

    /**
     * Polls UsbManager until both devices show up (USB devices can take a
     * moment to be registered) or the deadline passes, returning whatever
     * was found. A missing half comes back as null.
     */
    private fun findDevices(
        manager: UsbManager,
        kemperHint: String?,
        captainHint: String?
    ): Pair<UsbDevice?, UsbDevice?> {
        var kemper: UsbDevice? = null
        var captain: UsbDevice? = null
        val deadline = SystemClock.elapsedRealtime() + ENUMERATION_TIMEOUT_MS
        while (SystemClock.elapsedRealtime() < deadline) {
            val devices = manager.deviceList.values
            kemper = devices.firstOrNull {
                matches(it, kemperHint, KEMPER_VENDOR_IDS, "profiler", "kemper")
            }
            // The pedal's USB descriptor strings say "Raspberry Pi Pico"
            // (VID 0x239A), so on Android it shows as "Raspberry Pi Pico
            // Raspberry Pi Pico" - NOT "CircuitPython Audio" like on
            // desktop MIDI ports. Match pico/raspberry too.
            captain = devices.firstOrNull {
                matches(
                    it, captainHint, CAPTAIN_VENDOR_IDS,
                    "circuitpython", "captain", "pico", "raspberry"
                )
            }
            if (kemper != null && captain != null) break
            SystemClock.sleep(ENUMERATION_POLL_MS)
        }
        if (kemper == null || captain == null) {
            Log.d(
                TAG,
                "USB devices on the bus: " +
                    manager.deviceList.values.joinToString { deviceLabel(it) }
            )
        }
        return kemper to captain
    }

    /**
     * A device matches if its label (manufacturer + product name)
     * contains the hint when one is given, or any of the default name
     * patterns otherwise. Falls back to vendor-ID matching when the label
     * came back empty (string descriptors unavailable) - this can only
     * narrow to "the right kind of device", so it's a fallback, not the
     * primary signal, and does not apply when a hint was given (an
     * explicit hint that doesn't match a nameless device isn't a match).
     */
    private fun matches(
        device: UsbDevice,
        hint: String?,
        vendorIds: Set<Int>,
        vararg patterns: String
    ): Boolean {
        val label = deviceLabel(device)
        if (label.isNotBlank()) {
            if (!hint.isNullOrBlank()) return label.contains(hint, ignoreCase = true)
            return patterns.any { label.contains(it, ignoreCase = true) }
        }
        return hint.isNullOrBlank() && device.vendorId in vendorIds
    }

    /** Human-readable identity of a device: manufacturer + product name,
     * falling back to a VID/PID tag when USB string descriptors are empty
     * (can happen transiently right after attach, before enumeration
     * settles). */
    private fun deviceLabel(device: UsbDevice): String {
        val parts = listOfNotNull(device.manufacturerName, device.productName)
            .map { it.trim() }
            .filter { it.isNotEmpty() }
        return parts.joinToString(" ").ifEmpty {
            "USB device VID:%04X PID:%04X".format(device.vendorId, device.productId)
        }
    }

    private fun closeEverything() {
        kemperDevice?.close()
        captainDevice?.close()
        kemperDevice = null
        captainDevice = null
        detachReceiver?.let { r ->
            appContext?.let { ctx -> runCatching { ctx.unregisterReceiver(r) } }
        }
        detachReceiver = null
        bridgeThread?.quitSafely()
        bridgeThread = null
        bridgeHandler = null
    }
}
