# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.
#
# For more details, see
#   http://developer.android.com/guide/developing/tools/proguard.html

# If your project uses WebView with JS, uncomment the following
# and specify the fully qualified class name to the JavaScript interface
# class:
#-keepclassmembers class fqcn.of.javascript.interface.for.webview {
#   public *;
#}

# Uncomment this to preserve the line number information for
# debugging stack traces.
#-keepattributes SourceFile,LineNumberTable

# If you keep the line number information, uncomment this to
# hide the original source file name.
#-renamesourcefileattribute SourceFile
# BosunMidiBridge, BosunSerialBridge (and BosunSerialService) are only
# referenced from the Rust side through JNI (find_class + static method
# descriptors). R8 cannot see those references and would strip or rename
# the classes/methods in the release DEX, making the first JNI call throw
# NoClassDefFoundError/NoSuchMethodError (2026-08-15: exactly this crash on
# BosunSerialBridge.listPorts before this rule was added). Keep them with
# their original names - the JNI descriptors depend on the exact names.
-keep class com.bosun.app.BosunMidiBridge { *; }
-keep class com.bosun.app.BosunMidiBridge$* { *; }
-keep class com.bosun.app.BosunSerialBridge { *; }
-keep class com.bosun.app.BosunSerialService { *; }
