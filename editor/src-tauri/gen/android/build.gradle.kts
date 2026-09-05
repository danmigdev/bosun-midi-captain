import com.android.build.api.dsl.ApplicationExtension
import com.android.build.api.dsl.LibraryExtension

buildscript {
    repositories {
        google()
        mavenCentral()
    }
    dependencies {
        classpath("com.android.tools.build:gradle:8.11.0")
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:1.9.25")
    }
}

val serialPluginApiFloor = 26

allprojects {
    repositories {
        google()
        mavenCentral()
    }
}

// serialplugin 3.0.1 declares API 24 but directly calls the three-argument
// Context.registerReceiver overload introduced in API 26 (inside an API guard).
// Bosun itself requires API 29.  Give only this embedded dependency its real
// API floor after its own build script has run, so its lint remains enabled and
// Cargo's registry cache stays immutable.
project(":tauri-plugin-serialplugin").afterEvaluate {
    extensions.configure<LibraryExtension> {
        defaultConfig.minSdk = serialPluginApiFloor
    }
}

tasks.register("verifyAndroidSdkCompatibility") {
    group = "verification"
    description = "Checks that Bosun can safely embed serialplugin's Android API floor."
    doLast {
        val appMinSdk = project(":app")
            .extensions.getByType<ApplicationExtension>()
            .defaultConfig.minSdk
        val pluginMinSdk = project(":tauri-plugin-serialplugin")
            .extensions.getByType<LibraryExtension>()
            .defaultConfig.minSdk
        check(pluginMinSdk == serialPluginApiFloor) {
            "serialplugin minSdk=$pluginMinSdk, expected $serialPluginApiFloor"
        }
        check(appMinSdk != null && appMinSdk >= pluginMinSdk) {
            "Bosun minSdk=$appMinSdk cannot embed serialplugin minSdk=$pluginMinSdk"
        }
    }
}

tasks.register("clean").configure {
    delete("build")
}

