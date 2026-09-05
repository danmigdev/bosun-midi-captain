package com.bosun.app

/** Pure, synchronized ownership fence for JNI operations that may finish late. */
class SessionGenerationFence {
    private var latest: Long = 0
    private var active: Long = 0

    @Synchronized
    fun begin(generation: Long): Boolean {
        require(generation > 0)
        if (generation < latest) return false
        latest = generation
        active = 0
        return true
    }

    @Synchronized fun activate(generation: Long): Boolean {
        if (generation != latest) return false
        active = generation
        return true
    }

    @Synchronized fun owns(generation: Long): Boolean = generation == active

    @Synchronized fun release(generation: Long): Boolean {
        if (generation != active) return false
        active = 0
        return true
    }
}
