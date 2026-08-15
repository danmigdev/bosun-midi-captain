package com.bosun.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Local JVM unit tests for [BridgeHealthPolicy] - no emulator/device
 * needed (`./gradlew testDebugUnitTest`). Run BEFORE deploying any change
 * to this policy to a real device: the 2026-08-15 regression (bridge
 * restarting every ~15 s forever, tearing down a direction that was
 * working right along with the one that wasn't) shipped straight to
 * hardware with no test at all.
 */
class BridgeHealthPolicyTest {

    // ---- the actual 2026-08-15 regression, reproduced ----

    @Test
    fun `regression - a direction still completing its handshake is never restarted every 15s forever`() {
        // Exact failure shape: bridge starts, captain-to-kemper (c2k) works
        // immediately and keeps pinging every ~300ms (the beacon resend +
        // its own traffic), kemper-to-captain (k2c) never fires ONCE in this
        // simulated 5-minute window - i.e. the worst case, not just "a bit
        // slow". The buggy version restarted on a bare 15s clock with no
        // memory, so in 5 minutes (300s) it would have fired ~20 times
        // (every 15s past t=15s). With the grace period + cooldown, this
        // must fire only a small, BOUNDED number of times.
        var lastRestartAtMs = 0L
        var restarts = 0
        val bridgeStartedAtMs = 0L
        val lastC2kMs = { now: Long -> now }  // c2k always "just happened"

        var now = 0L
        while (now <= 300_000L) {
            val v = BridgeHealthPolicy.evaluate(
                nowMs = now,
                bridgeStartedAtMs = bridgeStartedAtMs,
                lastK2cMs = 0L,               // k2c: never once fired
                lastC2kMs = lastC2kMs(now),
                lastRestartAtMs = lastRestartAtMs,
            )
            if (v.shouldRestart) {
                restarts++
                lastRestartAtMs = now
            }
            now += 1_000L   // evaluated every second, worse than the real 15s cadence
        }

        // The buggy version: unbounded, ~1 restart per 15s once past the old
        // 15s grace = ~19 restarts in 300s. The fix must keep this small and
        // bounded by the cooldown: at most floor(300_000 / 60_000) + 1 = 6.
        assertTrue("expected a bounded restart count, got $restarts", restarts in 1..6)
    }

    @Test
    fun `within the startup grace period, a direction with zero messages is never flagged stale`() {
        val v = BridgeHealthPolicy.evaluate(
            nowMs = 20_000L,             // 20s since start...
            bridgeStartedAtMs = 0L,
            lastK2cMs = 0L,               // ...and k2c hasn't fired yet
            lastC2kMs = 20_000L,
            lastRestartAtMs = 0L,
        )
        // 20s < STARTUP_GRACE_MS (45s) - too early to judge, even though
        // 20s > STALE_AFTER_MS (15s) on its own would look stale.
        assertFalse(v.k2cStale)
        assertFalse(v.shouldRestart)
    }

    @Test
    fun `just past the startup grace period, a direction with zero messages IS flagged stale`() {
        val v = BridgeHealthPolicy.evaluate(
            nowMs = BridgeHealthPolicy.STARTUP_GRACE_MS + 1,
            bridgeStartedAtMs = 0L,
            lastK2cMs = 0L,
            lastC2kMs = BridgeHealthPolicy.STARTUP_GRACE_MS + 1,
            lastRestartAtMs = 0L,
        )
        assertTrue(v.k2cStale)
        assertFalse(v.c2kStale)
        assertTrue(v.shouldRestart)
    }

    // ---- steady-state: a direction that WAS working, then died ----

    @Test
    fun `a direction that goes silent well after startup is flagged stale`() {
        val bridgeStartedAtMs = 0L
        val lastK2cMs = 100_000L   // last seen at t=100s
        val now = lastK2cMs + BridgeHealthPolicy.STALE_AFTER_MS + 1
        val v = BridgeHealthPolicy.evaluate(
            nowMs = now,
            bridgeStartedAtMs = bridgeStartedAtMs,
            lastK2cMs = lastK2cMs,
            lastC2kMs = now,           // the other direction is fine
            lastRestartAtMs = 0L,
        )
        assertTrue(v.k2cStale)
        assertFalse(v.c2kStale)
        assertTrue(v.shouldRestart)
    }

    @Test
    fun `both directions active keeps the bridge alive indefinitely, never restarts`() {
        val bridgeStartedAtMs = 0L
        var now = 0L
        while (now <= 600_000L) {   // 10 simulated minutes
            val v = BridgeHealthPolicy.evaluate(
                nowMs = now,
                bridgeStartedAtMs = bridgeStartedAtMs,
                lastK2cMs = now,   // both directions "just fired" every tick
                lastC2kMs = now,
                lastRestartAtMs = 0L,
            )
            assertFalse("false positive at t=$now", v.shouldRestart)
            now += 500L   // ~ the real sensing-ping cadence
        }
    }

    // ---- the cooldown backstop, in isolation ----

    @Test
    fun `cooldown blocks a second restart recommendation immediately after the first`() {
        val bridgeStartedAtMs = 0L
        val firstRestartAt = BridgeHealthPolicy.STARTUP_GRACE_MS + 1

        val first = BridgeHealthPolicy.evaluate(
            nowMs = firstRestartAt,
            bridgeStartedAtMs = bridgeStartedAtMs,
            lastK2cMs = 0L,
            lastC2kMs = firstRestartAt,
            lastRestartAtMs = 0L,
        )
        assertTrue(first.shouldRestart)

        // Immediately after (simulating the caller having just restarted -
        // bridgeStartedAtMs would normally reset too, but this isolates the
        // cooldown guard specifically: even same-instant re-evaluation with
        // an unchanged still-stale k2c must not double-fire).
        val second = BridgeHealthPolicy.evaluate(
            nowMs = firstRestartAt + 1,
            bridgeStartedAtMs = bridgeStartedAtMs,
            lastK2cMs = 0L,
            lastC2kMs = firstRestartAt,
            lastRestartAtMs = firstRestartAt,   // caller recorded the restart
        )
        assertFalse(second.shouldRestart)
    }

    @Test
    fun `cooldown releases once MIN_RESTART_INTERVAL_MS has passed`() {
        val bridgeStartedAtMs = 0L
        val lastRestartAtMs = 100_000L
        val now = lastRestartAtMs + BridgeHealthPolicy.MIN_RESTART_INTERVAL_MS + 1
        val v = BridgeHealthPolicy.evaluate(
            nowMs = now,
            bridgeStartedAtMs = bridgeStartedAtMs,
            lastK2cMs = 0L,
            lastC2kMs = now,
            lastRestartAtMs = lastRestartAtMs,
        )
        assertTrue(v.shouldRestart)
    }

    @Test
    fun `neither direction stale means no restart even with zero cooldown elapsed`() {
        val v = BridgeHealthPolicy.evaluate(
            nowMs = 1_000_000L,
            bridgeStartedAtMs = 0L,
            lastK2cMs = 999_999L,
            lastC2kMs = 999_999L,
            lastRestartAtMs = 999_999L,
        )
        assertFalse(v.shouldRestart)
    }
}
