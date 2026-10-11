package com.bosun.app

import org.junit.Assert.*
import org.junit.Test

class UsbDeviceMatcherTest {
    private val kemperVendors = setOf(0x133E)
    private val kemperPatterns = listOf("profiler", "kemper")

    @Test fun `label joins trimmed names and is empty without any`() {
        assertEquals("PaintAudio MIDI Captain Bosun Native",
            UsbDeviceMatcher.label(" PaintAudio ", "MIDI Captain Bosun Native"))
        assertEquals("Profiler", UsbDeviceMatcher.label(null, "Profiler"))
        assertEquals("", UsbDeviceMatcher.label(null, null))
        assertEquals("", UsbDeviceMatcher.label("  ", ""))
    }

    @Test fun `a named device matches on its name, not its vendor`() {
        assertTrue(UsbDeviceMatcher.matches("Kemper Profiler", 0x133E, null, kemperVendors, kemperPatterns))
        assertFalse(UsbDeviceMatcher.matches("Some Interface", 0x133E, null, kemperVendors, kemperPatterns))
    }

    @Test fun `a nameless device falls back to its vendor ID`() {
        // The Player can enumerate before its string descriptors are readable.
        assertTrue(UsbDeviceMatcher.matches(UsbDeviceMatcher.label(null, null), 0x133E, null, kemperVendors, kemperPatterns))
        assertFalse(UsbDeviceMatcher.matches("", 0x2E8A, null, kemperVendors, kemperPatterns))
    }

    @Test fun `a hint decides for named devices and excludes nameless ones`() {
        assertTrue(UsbDeviceMatcher.matches("Kemper Profiler Stage", 0x133E, "stage", kemperVendors, kemperPatterns))
        assertFalse(UsbDeviceMatcher.matches("Kemper Profiler", 0x133E, "stage", kemperVendors, kemperPatterns))
        assertFalse(UsbDeviceMatcher.matches("", 0x133E, "stage", kemperVendors, kemperPatterns))
        assertTrue(UsbDeviceMatcher.matches("", 0x133E, " ", kemperVendors, kemperPatterns))
    }
}
