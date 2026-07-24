# Android Configuration

This file documents the manual changes needed in the generated Android project
after running `tauri android init`. These settings are not (yet) expressible in
tauri.conf.json.

## 1. Orientation

Add `android:screenOrientation="portrait"` to the main `<activity>` in
`gen/android/app/src/main/AndroidManifest.xml` to lock the app to portrait
on phones. For tablets, use `"fullSensor"` to allow both orientations.

```xml
<activity
    android:name=".MainActivity"
    android:screenOrientation="portrait"
    ...>
```

## 2. Foreground Service (serial keep-alive)

Create `gen/android/app/src/main/java/com/bosun/app/BosunSerialService.kt`:

```kotlin
package com.bosun.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder

class BosunSerialService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Serial Connection",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val tapIntent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, tapIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
            .setContentTitle("Bosun")
            .setContentText("Connected to MIDI Captain")
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()

        startForeground(NOTIFICATION_ID, notification)
        return START_STICKY
    }

    override fun onDestroy() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }

    companion object {
        const val CHANNEL_ID = "bosun_serial"
        const val NOTIFICATION_ID = 1001
    }
}
```

Register the service in `AndroidManifest.xml` inside the `<application>` block:

```xml
<service
    android:name=".BosunSerialService"
    android:foregroundServiceType="dataSync"
    android:exported="false" />
```

Add the foreground service permission:

```xml
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" />
```

## 3. USB Host Permission

Add USB device intent filter so the app auto-opens when the pedal is plugged in:

```xml
<intent-filter>
    <action android:name="android.hardware.usb.action.USB_DEVICE_ATTACHED" />
</intent-filter>

<meta-data
    android:name="android.hardware.usb.action.USB_DEVICE_ATTACHED"
    android:resource="@xml/device_filter" />
```

Create `gen/android/app/src/main/res/xml/device_filter.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- MIDI Captain (PaintAudio) - CDC ACM class -->
    <usb-device vendor-id="1209" product-id="0001" />
    <!-- Raspberry Pi Pico (CircuitPython) - CDC ACM class -->
    <usb-device vendor-id="0x2E8A" />
</resources>
```
