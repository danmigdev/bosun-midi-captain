//! Android USB-MIDI bridge: JNI front-end for the Kotlin `BosunMidiBridge`
//! singleton, which owns the Android `MidiManager`/`UsbManager` plumbing and
//! the actual Kemper (Player) <-> pedal relay.
//!
//! Exposes the same four commands as the desktop [`crate::midi`] module, so
//! the frontend cannot tell the platforms apart. Every Kotlin call runs on
//! the Android main thread via `tauri::wry::prelude::dispatch` (the same
//! mechanism Tauri's internal `run_on_android_context` uses); the result is
//! shipped back over a channel, so each command stays synchronous for the
//! frontend while all JNI access happens on the main thread.
//!
//! Kotlin contract - keep in sync with `BosunMidiBridge.kt`:
//!
//! ```kotlin
//! package com.bosun.app
//!
//! object BosunMidiBridge {
//!   data class BridgeStatus(val active: Boolean, val kemperPort: String?, val pedalPort: String?)
//!   data class MidiPorts(val inputs: Array<String>, val outputs: Array<String>)
//!
//!   @JvmStatic fun listPorts(context: Context): MidiPorts
//!   @JvmStatic fun start(context: Context, kemper: String?, pedal: String?): BridgeStatus
//!   @JvmStatic fun stop(context: Context): BridgeStatus
//!   @JvmStatic fun status(context: Context): BridgeStatus
//! }
//! ```
//!
//! JNI descriptors derived from that contract (the Kotlin data class getters
//! are `getActive()`, `getKemperPort()`, `getPedalPort()`, `getInputs()`,
//! `getOutputs()`).
#![cfg(target_os = "android")]

use std::sync::mpsc;

use jni::objects::{JClass, JObject, JObjectArray, JString, JValue};
use jni::JNIEnv;
use tauri::wry::prelude::dispatch;

use crate::midi::{BridgeStatus, MidiPorts};

/// JNI descriptor of the Kotlin singleton class and the static method
/// signatures it must expose. Keep in sync with `BosunMidiBridge.kt`.
const BRIDGE_CLASS: &str = "com/bosun/app/BosunMidiBridge";
const GET_APP_CONTEXT_SIG: &str = "()Landroid/content/Context;";
const LIST_PORTS_SIG: &str = "(Landroid/content/Context;)Lcom/bosun/app/BosunMidiBridge$MidiPorts;";
const START_SIG: &str = "(Landroid/content/Context;Ljava/lang/String;Ljava/lang/String;)Lcom/bosun/app/BosunMidiBridge$BridgeStatus;";
const STOP_SIG: &str = "(Landroid/content/Context;)Lcom/bosun/app/BosunMidiBridge$BridgeStatus;";
const STATUS_SIG: &str = "(Landroid/content/Context;)Lcom/bosun/app/BosunMidiBridge$BridgeStatus;";
const GET_ACTIVE_SIG: &str = "()Z";
const GET_STRING_SIG: &str = "()Ljava/lang/String;";
const GET_STRING_ARRAY_SIG: &str = "()[Ljava/lang/String;";

/// Run `f` on the Android main thread with a JNI env and the activity, then
/// wait for its result.
///
/// `dispatch` is wry's main-thread JNI pump - the same one Tauri's internal
/// `run_on_android_context` uses - so the env handed to `f` is already
/// attached and needs no attach/detach bookkeeping here. Commands run on
/// Tauri's thread pool, never on the main thread, so blocking on the channel
/// cannot deadlock; the main thread is free to run `f`.
///
/// Panics if no activity has been registered yet, but that can only happen
/// before the first webview exists, and commands can't be invoked before
/// then.
fn with_jni<F, T>(f: F) -> Result<T, String>
where
    F: FnOnce(&mut JNIEnv<'_>, &JObject<'_>) -> Result<T, String> + Send + 'static,
    T: Send + 'static,
{
    eprintln!("[jni] dispatch queued");
    let (tx, rx) = mpsc::channel();
    dispatch(move |env, activity, _webview| {
        // The command thread is blocked on rx while the main thread runs f.
        // The send can only fail if the command was cancelled, which is fine.
        eprintln!("[jni] main-thread closure started");
        let result = f(env, activity);
        eprintln!("[jni] main-thread closure done ok={}", result.is_ok());
        let _ = tx.send(result);
    });
    eprintln!("[jni] waiting for result");
    rx.recv().map_err(|_| "JNI dispatch channel closed".to_string())?
}

/// The app-wide `Context` for the Kotlin singleton. The activity is a Context
/// too, but the app context keeps the bridge alive across activity
/// recreations.
fn application_context<'local>(
    env: &mut JNIEnv<'local>,
    activity: &JObject<'_>,
) -> Result<JObject<'local>, String> {
    env.call_method(activity, "getApplicationContext", GET_APP_CONTEXT_SIG, &[])
        .and_then(|v| v.l())
        .map_err(|e| format!("getApplicationContext: {e}"))
}

/// The `BosunMidiBridge` class to call a static method on.
///
/// `env.find_class` from native code with no Java caller frame uses the
/// BOOT class loader, which cannot see app classes - it fails with
/// "Class not found using the boot class loader" and leaves the main
/// thread dead.  Load the class through the activity's class loader
/// instead (the standard JNI workaround).
fn bridge_class<'local>(
    env: &mut JNIEnv<'local>,
    activity: &JObject<'_>,
) -> Result<jni::objects::JClass<'local>, String> {
    let activity_class = env
        .get_object_class(activity)
        .map_err(|e| format!("get_object_class: {e}"))?;
    let loader = env
        .call_method(
            &activity_class,
            "getClassLoader",
            "()Ljava/lang/ClassLoader;",
            &[],
        )
        .and_then(|v| v.l())
        .map_err(|e| format!("getClassLoader: {e}"))?;
    let name = env
        .new_string(BRIDGE_CLASS)
        .map_err(|e| format!("new_string: {e}"))?;
    let cls = env
        .call_method(
            &loader,
            "loadClass",
            "(Ljava/lang/String;)Ljava/lang/Class;",
            &[JValue::Object(&name)],
        )
        .and_then(|v| v.l())
        .map_err(|e| format!("loadClass {BRIDGE_CLASS}: {e}"))?;
    Ok(JClass::from(cls))
}

/// Call a static `BosunMidiBridge` method that only takes the context and
/// decode the returned object with `read`.
fn call_bridge<T>(
    method: &'static str,
    sig: &'static str,
    read: fn(&mut JNIEnv<'_>, &JObject<'_>) -> Result<T, String>,
) -> Result<T, String>
where
    T: Send + 'static,
{
    with_jni(move |env, activity| {
        let context = application_context(env, activity)?;
        let class = bridge_class(env, activity)?;
        let value = env
            .call_static_method(class, method, sig, &[JValue::Object(&context)])
            .and_then(|v| v.l())
            .map_err(|e| format!("{method}: {e}"))?;
        read(env, &value)
    })
}

/// Optional MIDI port hint as a JNI argument: the hint `String`, or a null
/// reference when absent. The caller must keep the returned `JString` alive
/// until the static call that uses it completes (it is a local reference).
fn hint_arg<'local>(env: &mut JNIEnv<'local>, hint: &Option<String>) -> Result<JString<'local>, String> {
    match hint {
        Some(s) => env.new_string(s).map_err(|e| format!("hint string: {e}")),
        None => Ok(JString::from(JObject::null())),
    }
}

// --------------------- result decoding ---------------------

fn read_bridge_status(env: &mut JNIEnv<'_>, obj: &JObject<'_>) -> Result<BridgeStatus, String> {
    let active = env
        .call_method(obj, "getActive", GET_ACTIVE_SIG, &[])
        .and_then(|v| v.z())
        .map_err(|e| format!("getActive: {e}"))?;
    Ok(BridgeStatus {
        active,
        kemper_port: read_optional_string(env, obj, "getKemperPort")?,
        pedal_port: read_optional_string(env, obj, "getPedalPort")?,
    })
}

fn read_midi_ports(env: &mut JNIEnv<'_>, obj: &JObject<'_>) -> Result<MidiPorts, String> {
    Ok(MidiPorts {
        inputs: read_string_array(env, obj, "getInputs")?,
        outputs: read_string_array(env, obj, "getOutputs")?,
    })
}

/// Read a `String` getter result, mapping a JNI null reference to `None`.
fn read_optional_string(env: &mut JNIEnv<'_>, obj: &JObject<'_>, getter: &str) -> Result<Option<String>, String> {
    let value = env
        .call_method(obj, getter, GET_STRING_SIG, &[])
        .and_then(|v| v.l())
        .map_err(|e| format!("{getter}: {e}"))?;
    if value.as_raw().is_null() {
        Ok(None)
    } else {
        jstring_to_rust(env, &value).map(Some)
    }
}

/// Read a `String[]` getter result into a `Vec<String>`.
fn read_string_array(env: &mut JNIEnv<'_>, obj: &JObject<'_>, getter: &str) -> Result<Vec<String>, String> {
    let value = env
        .call_method(obj, getter, GET_STRING_ARRAY_SIG, &[])
        .and_then(|v| v.l())
        .map_err(|e| format!("{getter}: {e}"))?;
    let array = JObjectArray::from(value);
    let len = env.get_array_length(&array).map_err(|e| format!("{getter}: {e}"))?;
    let mut out = Vec::with_capacity(len as usize);
    for i in 0..len {
        let elem = env
            .get_object_array_element(&array, i)
            .map_err(|e| format!("{getter}[{i}]: {e}"))?;
        out.push(jstring_to_rust(env, &elem)?);
    }
    Ok(out)
}

fn jstring_to_rust(env: &mut JNIEnv<'_>, value: &JObject<'_>) -> Result<String, String> {
    // jni treats JString as a repr(transparent) view over JObject, so this
    // borrows without copying.
    let s = env
        .get_string(<&JString>::from(value))
        .map_err(|e| format!("string decode: {e}"))?;
    Ok(s.into())
}

// --------------------- commands ---------------------

/// Enumerate MIDI inputs and outputs via the Kotlin `BosunMidiBridge`.
#[tauri::command]
pub fn midi_list_ports() -> Result<MidiPorts, String> {
    call_bridge("listPorts", LIST_PORTS_SIG, read_midi_ports)
}

/// Open the Kemper <-> pedal relay in Kotlin. `kemper` / `pedal` are optional
/// substring hints; without them Kotlin auto-detects by device name.
#[tauri::command]
pub fn midi_bridge_start(
    kemper: Option<String>,
    pedal: Option<String>,
) -> Result<BridgeStatus, String> {
    with_jni(move |env, activity| {
        // Bind the hint strings before the call so the JNI local references
        // stay alive for its whole duration.
        let context = application_context(env, activity)?;
        let kemper_js = hint_arg(env, &kemper)?;
        let pedal_js = hint_arg(env, &pedal)?;
        let class = bridge_class(env, activity)?;
        let value = env
            .call_static_method(
                class,
                "start",
                START_SIG,
                &[
                    JValue::Object(&context),
                    JValue::Object(kemper_js.as_ref()),
                    JValue::Object(pedal_js.as_ref()),
                ],
            )
            .and_then(|v| v.l())
            .map_err(|e| format!("start: {e}"))?;
        read_bridge_status(env, &value)
    })
}

/// Tear down the relay in Kotlin.
#[tauri::command]
pub fn midi_bridge_stop() -> Result<BridgeStatus, String> {
    call_bridge("stop", STOP_SIG, read_bridge_status)
}

/// Report whether the Kotlin bridge is active and on which ports.
#[tauri::command]
pub fn midi_bridge_status() -> Result<BridgeStatus, String> {
    call_bridge("status", STATUS_SIG, read_bridge_status)
}
