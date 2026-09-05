package com.bosun.app

import org.junit.Assert.*
import org.junit.Test

class SessionGenerationFenceTest {
    @Test fun `late older session can never reclaim or release successor`() {
        val fence = SessionGenerationFence()
        assertTrue(fence.begin(1)); assertTrue(fence.activate(1))
        assertTrue(fence.begin(2)); assertTrue(fence.activate(2))
        assertFalse(fence.begin(1))
        assertFalse(fence.release(1))
        assertTrue(fence.owns(2))
    }

    @Test fun `same generation may reopen during recovery`() {
        val fence = SessionGenerationFence()
        assertTrue(fence.begin(7)); assertTrue(fence.activate(7))
        assertTrue(fence.begin(7)); assertFalse(fence.owns(7))
        assertTrue(fence.activate(7)); assertTrue(fence.owns(7))
    }

    @Test(expected = IllegalArgumentException::class)
    fun `zero generation is rejected`() { SessionGenerationFence().begin(0) }
}
