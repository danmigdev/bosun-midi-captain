package com.bosun.app

import android.content.Context
import android.media.midi.MidiDevice
import android.media.midi.MidiDeviceInfo
import android.media.midi.MidiInputPort
import android.media.midi.MidiManager
import android.media.midi.MidiReceiver
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/**
 * Bridges MIDI traffic between a Kemper device (Player or Profiler) and a
 * MIDI Captain pedal when both are plugged into the Android device through a
 * USB hub. Devices are discovered through the Android MIDI framework
 * (android.media.midi): USB-MIDI hardware attached in host mode is exposed
 * automatically by the system service.
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
 * "circuitpython"/"captain" for the pedal).
 */
object BosunMidiBridge {

    private const val TAG = "BosunMidiBridge"

    /** MIDI clock realtime message, noise for the pedal, always dropped. */
    private const val MIDI_CLOCK = 0xF8

    /** Active sensing realtime message, noise for the pedal, always dropped. */
    private const val MIDI_ACTIVE_SENSING = 0xFE

    /** Per-device open timeout in milliseconds. */
    private const val OPEN_TIMEOUT_MS = 3000L

    /** How long to wait for the USB MIDI devices to appear after start. */
    private const val ENUMERATION_TIMEOUT_MS = 2500L

    /** Poll interval used while waiting for device enumeration. */
    private const val ENUMERATION_POLL_MS = 100L

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

    /** Enumerated MIDI inputs and outputs visible to the system. */
    data class MidiPorts(
        val inputs: Array<String>,
        val outputs: Array<String>
    )

    // State below is only touched from start()/stop() (both synchronized) or
    // read as volatile fields by status() and the device removal callback.

    private var midiManager: MidiManager? = null
    private var bridgeThread: HandlerThread? = null
    private var bridgeHandler: Handler? = null

    /** App context saved at start(); used by the removal callback's stop(). */
    @Volatile private var appContext: Context? = null

    private val openDevices = ArrayList<MidiDevice>()
    private val openPorts = ArrayList<AutoCloseable>()

    private var callbackRegistered = false

    @Volatile private var active = false
    @Volatile private var kemperLabel: String? = null
    @Volatile private var captainLabel: String? = null

    /** SystemClock.elapsedRealtime() of the last message relayed in each
     *  direction, 0 = none yet this session. Per-direction because the
     *  bridge's own `active` flag only reflects whether start() succeeded -
     *  it stays true even if one direction's MidiReceiver callback stops
     *  being invoked while the other keeps working (observed live,
     *  2026-08-15: Kemper -> Captain relayed its first burst then produced
     *  zero further activity for the rest of the session while Captain ->
     *  Kemper kept working, and `active` never flipped false). The
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
     *  drops itself the moment it no longer matches: a self-heal restart
     *  calls start() again, which arms a NEW chain, and without this the
     *  OLD chain would keep polling forever alongside it too (active flips
     *  back true almost immediately, so the plain `if (!active) return`
     *  guard alone can't tell "stopped" apart from "superseded by a
     *  restart") - chains would stack without bound across repeated
     *  restarts, each firing its own log/eval every HEALTH_CHECK_POLL_MS. */
    @Volatile private var healthCheckGeneration: Int = 0

    /** How often the self-check evaluates BridgeHealthPolicy. Independent
     *  of the policy's own thresholds (STARTUP_GRACE_MS / STALE_AFTER_MS /
     *  MIN_RESTART_INTERVAL_MS, all in BridgeHealthPolicy.kt) - this is
     *  just the polling cadence, kept short so a genuine staleness is
     *  noticed promptly once the policy's own guards actually clear it. */
    private const val HEALTH_CHECK_POLL_MS = 15000L

    /** Ids of the bridged devices, used to react when one is unplugged. */
    @Volatile private var kemperInfoId = -1
    @Volatile private var captainInfoId = -1

    private val deviceCallback = object : MidiManager.DeviceCallback() {
        override fun onDeviceRemoved(device: MidiDeviceInfo) {
            if (device.id == kemperInfoId || device.id == captainInfoId) {
                Log.i(
                    TAG,
                    "Bridged MIDI device unplugged (${deviceLabel(device)}), stopping bridge"
                )
                // Do not call stop() synchronously: this callback may run on
                // the bridge handler thread while start() is awaiting an
                // openDevice callback delivered on that same thread, which
                // would deadlock the monitor. Tear down on a dedicated thread.
                val ctx = appContext
                Thread({
                    ctx?.let { runCatching { stop(it) } }
                }, "BosunMidiBridge-cleanup").start()
            }
        }
    }

    /** Enumerate the MIDI devices currently visible to the system. */
    @JvmStatic
    fun listPorts(context: Context): MidiPorts {
        val manager = context.applicationContext
            .getSystemService(Context.MIDI_SERVICE) as? MidiManager
            ?: return MidiPorts(emptyArray(), emptyArray())
        val inputs = ArrayList<String>()
        val outputs = ArrayList<String>()
        for (info in manager.devices) {
            val label = deviceLabel(info) ?: continue
            repeat(info.inputPortCount) { i -> inputs.add("$label IN $i") }
            repeat(info.outputPortCount) { i -> outputs.add("$label OUT $i") }
        }
        return MidiPorts(inputs.toTypedArray(), outputs.toTypedArray())
    }

    /**
     * Opens the Kemper and the Captain, wires Kemper IN to Captain OUT and
     * Captain IN to Kemper OUT, and returns the resulting status. Blocks the
     * calling thread (the Rust JNI thread) until both devices are open or the
     * timeouts expire, so the returned status is final.
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
            .getSystemService(Context.MIDI_SERVICE) as? MidiManager
        if (manager == null) {
            Log.e(TAG, "MidiManager service not available")
            return BridgeStatus(false, null, null)
        }
        midiManager = manager

        ensureBridgeThread()
        val handler = bridgeHandler ?: return BridgeStatus(false, null, null)

        val (kemperInfo, captainInfo) = findDevices(manager, kemperHint, captainHint)
        if (kemperInfo == null || captainInfo == null) {
            Log.e(
                TAG,
                "MIDI bridge needs both devices. Kemper: ${deviceLabel(kemperInfo) ?: "not found"}" +
                    " (hint: ${kemperHint ?: "none"}), " +
                    "Captain: ${deviceLabel(captainInfo) ?: "not found"}" +
                    " (hint: ${captainHint ?: "none"})"
            )
            return BridgeStatus(false, deviceLabel(kemperInfo), deviceLabel(captainInfo))
        }

        val kemper = openDevice(manager, kemperInfo, handler)
        val captain = openDevice(manager, captainInfo, handler)
        if (kemper == null || captain == null) {
            Log.e(TAG, "Failed to open the MIDI devices")
            closeEverything()
            return BridgeStatus(false, deviceLabel(kemperInfo), deviceLabel(captainInfo))
        }
        openDevices.add(kemper)
        openDevices.add(captain)

        // Kemper -> Captain, Captain -> Kemper.
        Log.i(
            TAG,
            "Kemper ports in=${kemperInfo.inputPortCount} out=${kemperInfo.outputPortCount}; " +
                "Captain ports in=${captainInfo.inputPortCount} out=${captainInfo.outputPortCount}"
        )
        val k2c = wire(kemper, captain) { lastK2cMs = SystemClock.elapsedRealtime() }
        val c2k = wire(captain, kemper) { lastC2kMs = SystemClock.elapsedRealtime() }
        Log.i(TAG, "Bridge links: kemper->captain=$k2c captain->kemper=$c2k")

        if (k2c + c2k == 0) {
            Log.e(TAG, "Could not open any MIDI port on the bridged devices")
            closeEverything()
            return BridgeStatus(false, deviceLabel(kemperInfo), deviceLabel(captainInfo))
        }

        kemperInfoId = kemperInfo.id
        captainInfoId = captainInfo.id
        manager.registerDeviceCallback(deviceCallback, handler)
        callbackRegistered = true

        active = true
        kemperLabel = deviceLabel(kemperInfo)
        captainLabel = deviceLabel(captainInfo)
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

    /**
     * Periodic self-check: if EITHER direction has relayed nothing since
     * before BridgeHealthPolicy's startup grace period, that direction's
     * MidiReceiver callback has effectively died (see the
     * FilteringForwarder.onSend comment) even though `active` itself never
     * flips false - the coarse app-level health check (App.svelte polling
     * midi_bridge_status()) cannot see this split-brain state, only a
     * from-inside check that knows about both directions can.
     *
     * The actual grace/staleness/cooldown judgment lives in
     * BridgeHealthPolicy (unit-tested, see BridgeHealthPolicyTest) - this
     * function only supplies the live clock readings and acts on the
     * verdict. Self-heals with a full stop+start using the same auto-detect
     * name patterns (no stored hints - by the time this fires, whatever the
     * user originally typed no longer matters, only that both devices are
     * still on the bus).
     */
    private fun scheduleHealthCheck(handler: Handler, generation: Int) {
        handler.postDelayed(object : Runnable {
            override fun run() {
                // Either the bridge was stopped, or a self-heal restart
                // (below) armed a newer chain - either way, drop this one.
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
                // DETECTION ONLY - the auto-restart that briefly lived here
                // (verdict.shouldRestart -> stop()+start() on a dedicated
                // thread) was itself a regression (2026-08-15, second one):
                // triggered right as the Kemper had genuinely been unplugged,
                // start()'s findDevices() poll re-requested USB permission
                // for it, and Android re-showed the permission dialog on
                // every retry - while the main thread was also blocked
                // waiting on the BosunMidiBridge object's @Synchronized
                // lock (held by this restart thread), the app went
                // unresponsive (ANR: "Bosun isn't responding") with a blank
                // WebView. The grace/cooldown math in BridgeHealthPolicy is
                // right and stays wired in for the log line above, but
                // actually calling start() again from here needs a real
                // design for "device permission may be needed again" and
                // "don't contend with the main thread for the object lock"
                // before it's safe to re-enable - not attempted yet.
                handler.postDelayed(this, HEALTH_CHECK_POLL_MS)
            }
        }, HEALTH_CHECK_POLL_MS)
    }

    /** Closes every MIDI device and port and marks the bridge inactive.
     * The Context parameter exists purely for the JNI signature parity with
     * listPorts/start - the bridge itself does not need it. */
    @JvmStatic
    @Synchronized
    fun stop(context: Context): BridgeStatus {
        if (active || openDevices.isNotEmpty()) {
            Log.i(TAG, "Stopping MIDI bridge")
            // Clear the ids first so a queued removal callback cannot
            // re-trigger a stop while we are tearing down.
            kemperInfoId = -1
            captainInfoId = -1
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
     * Polls MidiManager until both devices show up (USB devices can take a
     * moment to be registered) or the deadline passes, returning whatever was
     * found. A missing half comes back as null.
     */
    private fun findDevices(
        manager: MidiManager,
        kemperHint: String?,
        captainHint: String?
    ): Pair<MidiDeviceInfo?, MidiDeviceInfo?> {
        var kemper: MidiDeviceInfo? = null
        var captain: MidiDeviceInfo? = null
        val deadline = SystemClock.elapsedRealtime() + ENUMERATION_TIMEOUT_MS
        while (SystemClock.elapsedRealtime() < deadline) {
            val devices = manager.devices
            kemper = devices.firstOrNull { matchesLabel(it, kemperHint, "profiler", "kemper") }
            // The pedal's USB descriptor strings say "Raspberry Pi Pico"
            // (VID 0x239A), so on Android it shows as "Raspberry Pi Pico
            // Raspberry Pi Pico" - NOT "CircuitPython Audio" like on
            // desktop MIDI ports. Match pico/raspberry too.
            captain = devices.firstOrNull {
                matchesLabel(
                    it, captainHint,
                    "circuitpython", "captain", "pico", "raspberry"
                )
            }
            if (kemper != null && captain != null) break
            SystemClock.sleep(ENUMERATION_POLL_MS)
        }
        if (kemper == null || captain == null) {
            Log.d(
                TAG,
                "MIDI devices on the bus: " +
                    manager.devices.joinToString { deviceLabel(it) ?: "?" }
            )
        }
        return kemper to captain
    }

    /**
     * A device matches if its label (name, manufacturer and product
     * concatenated) contains the hint when one is given, or any of the
     * default name patterns otherwise.
     */
    private fun matchesLabel(info: MidiDeviceInfo, hint: String?, vararg patterns: String): Boolean {
        val label = deviceLabel(info) ?: return false
        if (!hint.isNullOrBlank() && label.contains(hint, ignoreCase = true)) return true
        return patterns.any { label.contains(it, ignoreCase = true) }
    }

    /** Human-readable identity of a device, or null for a null info. */
    private fun deviceLabel(info: MidiDeviceInfo?): String? {
        if (info == null) return null
        val props = info.properties
        val parts = listOf(
            props[MidiDeviceInfo.PROPERTY_NAME],
            props[MidiDeviceInfo.PROPERTY_MANUFACTURER],
            props[MidiDeviceInfo.PROPERTY_PRODUCT]
        )
            .mapNotNull { it as? String }
            .map { it.trim() }
            .filter { it.isNotEmpty() }
        return parts.joinToString(" ").ifEmpty { "MIDI device #${info.id}" }
    }

    /** Opens a device, blocking until the callback fires or the timeout ends. */
    private fun openDevice(
        manager: MidiManager,
        info: MidiDeviceInfo,
        handler: Handler
    ): MidiDevice? {
        val latch = CountDownLatch(1)
        val deviceRef = AtomicReference<MidiDevice?>()
        manager.openDevice(info, { device ->
            deviceRef.set(device)
            latch.countDown()
        }, handler)
        val opened = try {
            latch.await(OPEN_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }
        if (!opened) {
            // The callback may still fire after the timeout; close it so it
            // does not leak an untracked device.
            deviceRef.get()?.let { runCatching { it.close() } }
            Log.w(TAG, "Timed out opening ${deviceLabel(info)}")
            return null
        }
        return deviceRef.get()
    }

    /**
     * Wires the [from] device's OUTPUT ports (data received FROM the device,
     * via MidiSender.connect) into the [to] device's INPUT ports (data sent
     * TO the device, via the MidiReceiver.send inherited by MidiInputPort).
     * Returns the number of port links created.
     *
     * Android MIDI naming is from the DEVICE's perspective:
     * - MidiOutputPort = data the device produces -> connect() a receiver
     * - MidiInputPort  = data the device consumes -> call send() on it
     *
     * [onMessage] fires on every message actually relayed this direction -
     * the per-direction liveness signal the health-check below watches.
     */
    private fun wire(from: MidiDevice, to: MidiDevice, onMessage: () -> Unit): Int {
        val inputs = ArrayList<MidiInputPort>()
        for (i in 0 until to.info.inputPortCount) {
            val input = to.openInputPort(i) ?: continue
            openPorts.add(input)
            inputs.add(input)
        }
        if (inputs.isEmpty()) return 0

        var links = 0
        for (i in 0 until from.info.outputPortCount) {
            val output = from.openOutputPort(i) ?: continue
            openPorts.add(output)
            output.connect(
                FilteringForwarder(
                    inputs,
                    "${deviceLabel(from.info)} -> ${deviceLabel(to.info)}",
                    onMessage
                )
            )
            links += 1
        }
        return links
    }

    /**
     * Forwards MIDI messages to the target device input ports, dropping MIDI
     * clock (0xF8) and active sensing (0xFE) bytes. Those are standalone
     * one-byte realtime messages, so skipping them never corrupts the
     * surrounding stream.
     */
    private class FilteringForwarder(
        private val targets: List<MidiInputPort>,
        private val label: String,
        private val onMessage: () -> Unit
    ) : MidiReceiver() {

        private var messageCount = 0

        override fun onSend(data: ByteArray, offset: Int, count: Int, timestamp: Long) {
            // Whole body wrapped: this runs on the MIDI framework's own
            // dispatch thread, and an uncaught throw here (malformed
            // data/offset/count, an unexpected array bound, ...) has no
            // visible AndroidRuntime crash to point at - it can just go
            // silent. Observed live (2026-08-15): the Kemper -> Captain
            // direction relayed its first burst of messages successfully
            // (confirmed via the pedal's own CONTEXT stream picking up a
            // rig name) then produced zero further activity for the rest
            // of the session while Captain -> Kemper kept working - exactly
            // the shape of one direction's callback dying without a trace.
            // A caught-and-logged exception here turns that silence into a
            // visible log line the next time this happens.
            try {
                messageCount += 1
                onMessage()
                if (messageCount <= 3) {
                    val hex = (offset until (offset + count).coerceAtMost(offset + 12))
                        .joinToString(" ") { "%02x".format(data[it].toInt() and 0xFF) }
                    Log.i(TAG, "[$label] rx #$messageCount: $hex")
                }
                // Logged every 50 (not just once) so a stalled direction is
                // visible as a stale timestamp on its last heartbeat line,
                // next to the other direction's still-advancing count.
                if (messageCount % 50 == 0) {
                    Log.i(TAG, "[$label] $messageCount messages relayed so far")
                }
                var containsNoise = false
                for (i in offset until offset + count) {
                    val b = data[i].toInt() and 0xFF
                    if (b == MIDI_CLOCK || b == MIDI_ACTIVE_SENSING) {
                        containsNoise = true
                        break
                    }
                }
                if (!containsNoise) {
                    forward(data, offset, count)
                    return
                }
                // Copy the buffer, skipping the noise bytes.
                val filtered = ByteArray(count)
                var out = 0
                for (i in offset until offset + count) {
                    val b = data[i].toInt() and 0xFF
                    if (b != MIDI_CLOCK && b != MIDI_ACTIVE_SENSING) {
                        filtered[out++] = data[i]
                    }
                }
                if (out > 0) forward(filtered, 0, out)
            } catch (e: Exception) {
                Log.e(TAG, "[$label] onSend crashed on message #$messageCount (${count}B) - this direction may now be dead", e)
            }
        }

        private fun forward(data: ByteArray, offset: Int, count: Int) {
            for (target in targets) {
                try {
                    // MidiInputPort inherits MidiReceiver.send() - this is
                    // what pushes data INTO the destination device.
                    target.send(data, offset, count)
                } catch (e: Exception) {
                    // send() declares IOException (message too large for the
                    // port, buffer full, ...). Catching only RuntimeException
                    // let those escape into the MIDI dispatcher, which drops
                    // the link silently - every forward must be swallowed.
                    Log.w(TAG, "MIDI forward failed (${count}B)", e)
                }
            }
        }
    }

    private fun closeEverything() {
        openPorts.forEach { runCatching { it.close() } }
        openPorts.clear()
        openDevices.forEach { runCatching { it.close() } }
        openDevices.clear()
        if (callbackRegistered) {
            midiManager?.unregisterDeviceCallback(deviceCallback)
            callbackRegistered = false
        }
        midiManager = null
        bridgeThread?.quitSafely()
        bridgeThread = null
        bridgeHandler = null
    }
}
