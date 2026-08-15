package com.bosun.app

/**
 * Pure decision logic for BosunMidiBridge's per-direction health check.
 *
 * Deliberately free of any Android dependency (SystemClock, Handler, ...) -
 * every input is a plain monotonic-millisecond Long the caller supplies, so
 * this can be exercised by a local JVM unit test (app/src/test/...) without
 * an emulator or device. BosunMidiBridge itself only wires this to
 * SystemClock.elapsedRealtime() and acts on the verdict.
 *
 * History: the first version of this health check lived inline in
 * BosunMidiBridge and restarted the bridge the instant a direction looked
 * stale, checked on a fixed 15 s clock with no memory of previous restarts.
 * Shipped straight to a real device without a test, it turned out to
 * restart the bridge every ~15 s forever (2026-08-15) - the Kemper's first
 * sensing reply can legitimately take longer than 15 s to arrive under a
 * busy link, so the "never proven itself yet" case looked identical to a
 * "was working, then died" case, and every restart reset the clock before
 * the slow-but-fine case ever got to finish. That also tore down the OTHER
 * direction, which WAS working, right along with it - every cycle. This
 * rewrite adds the two guards that make that specific failure structurally
 * impossible: a startup grace period much longer than the steady-state
 * staleness window, and a hard minimum interval between restarts that holds
 * even if every other guard here turns out to have its own bug.
 */
object BridgeHealthPolicy {
    /** Once a direction has had its fair chance to prove itself (past the
     *  startup grace period), this is how long it may go with no message
     *  before being judged stale. Matches kemper.py's own
     *  _SENSING_TIMEOUT_MS, since it's the same underlying judgment call
     *  ("is this Kemper subscription still alive") from the other end of
     *  the same link. */
    const val STALE_AFTER_MS = 15_000L

    /** How long after a (re)start a direction is given before it's even
     *  eligible to be judged stale. Deliberately much longer than
     *  STALE_AFTER_MS: a COLD start (device enumeration, port opening, the
     *  initial beacon round-trip) is slower than an ESTABLISHED link simply
     *  going quiet, and conflating the two is exactly what caused the
     *  2026-08-15 regression. */
    const val STARTUP_GRACE_MS = 45_000L

    /** Hard floor on how often a restart may fire, regardless of how stale
     *  things look. The backstop: even a startup-grace bug, a units bug, or
     *  a bad interaction with some future change cannot reproduce the
     *  infinite-restart-every-15s regression while this holds, because no
     *  amount of "looks stale" can shrink this floor. */
    const val MIN_RESTART_INTERVAL_MS = 60_000L

    data class Verdict(
        val k2cStale: Boolean,
        val c2kStale: Boolean,
        /** True only when at least one direction is stale AND the cooldown
         *  since the last restart has elapsed. */
        val shouldRestart: Boolean,
    )

    /**
     * @param nowMs monotonic "now" (SystemClock.elapsedRealtime() at the caller)
     * @param bridgeStartedAtMs when the current bridge instance came up
     * @param lastK2cMs last Kemper->Captain message, 0 = none yet this instance
     * @param lastC2kMs last Captain->Kemper message, 0 = none yet this instance
     * @param lastRestartAtMs when this policy last recommended a restart that
     *   was acted on, 0 = never. The caller is responsible for updating this
     *   only when it actually restarts - the policy is otherwise stateless.
     */
    fun evaluate(
        nowMs: Long,
        bridgeStartedAtMs: Long,
        lastK2cMs: Long,
        lastC2kMs: Long,
        lastRestartAtMs: Long,
    ): Verdict {
        val pastGrace = nowMs - bridgeStartedAtMs > STARTUP_GRACE_MS
        val k2cStale = pastGrace && (lastK2cMs == 0L || nowMs - lastK2cMs > STALE_AFTER_MS)
        val c2kStale = pastGrace && (lastC2kMs == 0L || nowMs - lastC2kMs > STALE_AFTER_MS)
        val cooldownElapsed = lastRestartAtMs == 0L || nowMs - lastRestartAtMs > MIN_RESTART_INTERVAL_MS
        return Verdict(
            k2cStale = k2cStale,
            c2kStale = c2kStale,
            shouldRestart = (k2cStale || c2kStale) && cooldownElapsed,
        )
    }
}
