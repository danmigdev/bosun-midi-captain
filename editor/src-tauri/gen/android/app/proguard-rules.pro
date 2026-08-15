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
# BosunMidiBridge (and BosunSerialService) are only referenced from the Rust
# side through JNI (find_class + static method descriptors). R8 cannot see
# those references and would strip the classes from the release DEX, making
# the first JNI call throw NoClassDefFoundError. Keep them with their
# original names - the JNI descriptors depend on the exact class names.
-keep class com.bosun.app.BosunMidiBridge { *; }
-keep class com.bosun.app.BosunMidiBridge$* { *; }
-keep class com.bosun.app.BosunSerialService { *; }
