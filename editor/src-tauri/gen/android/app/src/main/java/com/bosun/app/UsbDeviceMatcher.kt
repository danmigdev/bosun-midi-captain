package com.bosun.app

/**
 * Decides whether a USB device is the one a bridge is looking for, from its
 * string descriptors and vendor ID. Free of Android types so it runs in JVM
 * unit tests; BosunMidiBridge and BosunSerialBridge pass the UsbDevice
 * fields in.
 */
object UsbDeviceMatcher {
    /** Manufacturer and product name joined, or "" when the device reported
     * neither (string descriptors unavailable). */
    fun label(manufacturer: String?, product: String?): String =
        listOfNotNull(manufacturer, product)
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .joinToString(" ")

    /**
     * A device matches if its label contains the hint when one is given, or
     * any of `patterns` otherwise. With an empty label only the vendor ID is
     * left: it can only narrow to "the right kind of device", so it is a
     * fallback, and it does not apply when a hint was given (an explicit hint
     * that doesn't match a nameless device isn't a match).
     */
    fun matches(
        label: String,
        vendorId: Int,
        hint: String?,
        vendorIds: Set<Int>,
        patterns: List<String>,
    ): Boolean {
        if (label.isNotBlank()) {
            if (!hint.isNullOrBlank()) return label.contains(hint, ignoreCase = true)
            return patterns.any { label.contains(it, ignoreCase = true) }
        }
        return hint.isNullOrBlank() && vendorId in vendorIds
    }
}
