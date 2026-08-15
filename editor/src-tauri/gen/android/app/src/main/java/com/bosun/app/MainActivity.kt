package com.bosun.app

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.activity.enableEdgeToEdge
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding

class MainActivity : TauriActivity() {

    companion object {
        private const val TAG = "BosunMain"
        private const val ACTION_USB_PERMISSION = "com.bosun.app.USB_PERMISSION"
        /** VIDs matched by res/xml/device_filter.xml -- keep in sync. */
        private val BOSUN_VENDOR_IDS = setOf(0x239A, 0x2E8A, 0x133E)
    }

    private var usbPermissionIntent: PendingIntent? = null
    private var usbReceiver: BroadcastReceiver? = null
    private var requestedDevice: UsbDevice? = null
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)

        ViewCompat.setOnApplyWindowInsetsListener(findViewById(android.R.id.content)) { view, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.updatePadding(
                top = systemBars.top,
                bottom = systemBars.bottom,
                left = systemBars.left,
                right = systemBars.right
            )
            WindowInsetsCompat.CONSUMED
        }

        usbPermissionIntent = PendingIntent.getBroadcast(
            this, 0,
            Intent(ACTION_USB_PERMISSION),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        usbReceiver = UsbPermissionReceiver()
        ContextCompat.registerReceiver(
            this, usbReceiver, IntentFilter(ACTION_USB_PERMISSION),
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (UsbManager.ACTION_USB_DEVICE_ATTACHED == intent.action) {
            Log.i(TAG, "USB_DEVICE_ATTACHED intent received")
            // Device was just plugged in -- give it a moment to be enumerated,
            // then check.  postDelayed so the activity window is definitely
            // attached when requestPermission() shows its dialog.
            handler.postDelayed({ checkAndRequestUsbPermission() }, 300)
        }
    }

    override fun onResume() {
        super.onResume()
        // Poll every 2 seconds for the first 30 seconds after launch.
        // Android's USB host enumeration can take a variable amount of
        // time depending on the device and bus state.
        for (i in 0..14) {
            handler.postDelayed({
                Log.d(TAG, "poll #${i}: checking USB devices")
                checkAndRequestUsbPermission()
            }, 500L + i * 2000L)
        }
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        usbReceiver?.let { unregisterReceiver(it) }
        super.onDestroy()
    }

    private fun checkAndRequestUsbPermission() {
        val manager = getSystemService(Context.USB_SERVICE) as? UsbManager
        if (manager == null) {
            Log.w(TAG, "UsbManager not available")
            return
        }
        val intent = usbPermissionIntent ?: return

        val deviceList = manager.deviceList
        Log.d(TAG, "deviceList has ${deviceList.size} device(s)")

        for (device in deviceList.values) {
            val vid = device.vendorId
            val name = device.deviceName
            Log.d(TAG, "  device: name=$name vid=$vid pid=${device.productId}")

            if (vid in BOSUN_VENDOR_IDS) {
                if (manager.hasPermission(device)) {
                    Log.i(TAG, "USB permission already held for $name")
                    continue
                }
                if (device == requestedDevice) {
                    Log.d(TAG, "Permission already requested for $name, waiting for user response")
                    return
                }
                requestedDevice = device
                Log.i(TAG, "Requesting USB permission for $name (VID $vid)")
                manager.requestPermission(device, intent)
                return
            }
        }
    }

    // ---- permission result ----

    private inner class UsbPermissionReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action != ACTION_USB_PERMISSION) return
            val device = requestedDevice
            requestedDevice = null

            val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
            val name = device?.deviceName ?: "unknown"
            Log.i(TAG, "USB permission result: granted=$granted device=$name")

            if (granted && device != null) {
                Log.i(TAG, "Permission granted for $name -- serial port should be ready now")
            }
        }
    }
}
