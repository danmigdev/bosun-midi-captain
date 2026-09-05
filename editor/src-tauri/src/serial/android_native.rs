//! JNI front-end for the Kotlin `BosunSerialBridge` singleton, which owns a
//! raw-USB CDC-ACM connection to the Captain's data port via
//! `UsbDeviceConnection.bulkTransfer()` - bypassing
//! `tauri-plugin-serialplugin`'s Android backend entirely. See
//! `BosunSerialDevice.kt`'s doc comment for why: that backend's own
//! per-call timeout is not reliably honored by Android's USB host stack (a
//! well-documented platform limitation), which was causing frequent
//! multi-second read/write hangs even after `android.rs`'s own
//! `call_with_timeout` watchdog made them survivable rather than fatal.
//!
//! Wry's main-thread dispatch is used once to cache the JavaVM and a global
//! activity reference. Actual Kotlin calls run on attached Rust worker
//! threads, keeping every blocking USB operation off Android's UI thread.
//!
//! Kotlin contract - keep in sync with `BosunSerialBridge.kt`:
//!
//! ```kotlin
//! package com.bosun.app
//!
//! object BosunSerialBridge {
//!   const val PORT_NAME = "usb-data"
//!   @JvmStatic fun listPorts(context: Context): Array<String>
//!   @JvmStatic fun open(context: Context, port: String, generation: Long): String
//!   @JvmStatic fun close(context: Context, generation: Long)
//!   @JvmStatic fun read(generation: Long, maxLen: Int, timeoutMs: Int): ByteArray
//!   @JvmStatic fun write(generation: Long, data: ByteArray, timeoutMs: Int): Int
//! }
//! ```
#![cfg(target_os = "android")]

use std::sync::{mpsc, OnceLock};

use jni::objects::{GlobalRef, JByteArray, JClass, JObject, JObjectArray, JString, JThrowable, JValue};
use jni::{JNIEnv, JavaVM};
use tauri::wry::prelude::dispatch;

const BRIDGE_CLASS: &str = "com/bosun/app/BosunSerialBridge";
const GET_APP_CONTEXT_SIG: &str = "()Landroid/content/Context;";
const LIST_PORTS_SIG: &str = "(Landroid/content/Context;)[Ljava/lang/String;";
const OPEN_SIG: &str = "(Landroid/content/Context;Ljava/lang/String;J)Ljava/lang/String;";
const CLOSE_SIG: &str = "(Landroid/content/Context;J)V";
const READ_SIG: &str = "(JII)[B";
const WRITE_SIG: &str = "(J[BI)I";
const GET_MESSAGE_SIG: &str = "()Ljava/lang/String;";

struct JniContext {
    vm: JavaVM,
    activity: GlobalRef,
}

static JNI_CONTEXT: OnceLock<JniContext> = OnceLock::new();

/// Cache only the VM/activity handles on Android's UI thread, then execute
/// every actual bridge call on the Rust caller thread attached to the VM.
/// This is critical: UsbDeviceConnection bulk I/O must never run inside
/// wry's main-thread dispatch callback.
fn with_jni<F, T>(f: F) -> Result<T, String>
where
    F: FnOnce(&mut JNIEnv<'_>, &JObject<'_>) -> Result<T, String> + Send + 'static,
    T: Send + 'static,
{
    if JNI_CONTEXT.get().is_none() {
        let (tx, rx) = mpsc::channel();
        dispatch(move |env, activity, _webview| {
            let result = env.get_java_vm()
                .map_err(|e| format!("get JavaVM: {e}"))
                .and_then(|vm| env.new_global_ref(activity)
                    .map(|activity| JniContext { vm, activity })
                    .map_err(|e| format!("global activity ref: {e}")));
            let _ = tx.send(result);
        });
        let context = rx.recv().map_err(|_| "JNI bootstrap channel closed".to_string())??;
        let _ = JNI_CONTEXT.set(context);
    }
    let context = JNI_CONTEXT.get().ok_or_else(|| "JNI context unavailable".to_string())?;
    let mut env = context.vm.attach_current_thread()
        .map_err(|e| format!("attach JNI worker: {e}"))?;
    f(&mut env, context.activity.as_obj())
}

fn application_context<'local>(
    env: &mut JNIEnv<'local>,
    activity: &JObject<'_>,
) -> Result<JObject<'local>, String> {
    env.call_method(activity, "getApplicationContext", GET_APP_CONTEXT_SIG, &[])
        .and_then(|v| v.l())
        .map_err(|e| format!("getApplicationContext: {e}"))
}

/// Loaded through the activity's class loader - `env.find_class` from
/// native code with no Java caller frame uses the boot class loader, which
/// cannot see app classes. See `midi_android.rs::bridge_class`.
fn bridge_class<'local>(
    env: &mut JNIEnv<'local>,
    activity: &JObject<'_>,
) -> Result<JClass<'local>, String> {
    let activity_class = env
        .get_object_class(activity)
        .map_err(|e| format!("get_object_class: {e}"))?;
    let loader = env
        .call_method(&activity_class, "getClassLoader", "()Ljava/lang/ClassLoader;", &[])
        .and_then(|v| v.l())
        .map_err(|e| format!("getClassLoader: {e}"))?;
    let name = env
        .new_string(BRIDGE_CLASS)
        .map_err(|e| format!("new_string: {e}"))?;
    let cls = env
        .call_method(&loader, "loadClass", "(Ljava/lang/String;)Ljava/lang/Class;", &[JValue::Object(&name)])
        .and_then(|v| v.l())
        .map_err(|e| format!("loadClass {BRIDGE_CLASS}: {e}"))?;
    Ok(JClass::from(cls))
}

/// After a call that may have thrown (BosunSerialBridge's open/read/write
/// all throw on failure rather than returning a Result-shaped object, the
/// more idiomatic Kotlin style for this bridge), check for and consume a
/// pending JNI exception, returning its message as a plain Err. Must be
/// called immediately after any `call_static_method`/`call_method` that
/// could have thrown - a pending exception poisons every further JNI call
/// on this env until cleared.
fn take_pending_exception(env: &mut JNIEnv<'_>) -> Result<(), String> {
    if !env.exception_check().unwrap_or(false) {
        return Ok(());
    }
    let throwable: JThrowable = match env.exception_occurred() {
        Ok(t) => t,
        Err(e) => {
            let _ = env.exception_clear();
            return Err(format!("exception occurred but could not be retrieved: {e}"));
        }
    };
    let _ = env.exception_clear();
    let message = env
        .call_method(&throwable, "getMessage", GET_MESSAGE_SIG, &[])
        .and_then(|v| v.l())
        .ok()
        .and_then(|obj| {
            if obj.as_raw().is_null() {
                None
            } else {
                jstring_to_rust(env, &obj).ok()
            }
        })
        .unwrap_or_else(|| "(no exception message)".to_string());
    Err(message)
}

fn jstring_to_rust(env: &mut JNIEnv<'_>, value: &JObject<'_>) -> Result<String, String> {
    let s = env
        .get_string(<&JString>::from(value))
        .map_err(|e| format!("string decode: {e}"))?;
    Ok(s.into())
}

// --------------------- commands ---------------------

/// Enumerate the synthetic port list ("usb-data" if the Captain is present
/// on the bus, empty otherwise).
pub fn available_ports() -> Result<Vec<String>, String> {
    with_jni(move |env, activity| {
        let context = application_context(env, activity)?;
        let class = bridge_class(env, activity)?;
        let value = env
            .call_static_method(class, "listPorts", LIST_PORTS_SIG, &[JValue::Object(&context)])
            .and_then(|v| v.l())
            .map_err(|e| format!("listPorts: {e}"))?;
        take_pending_exception(env)?;
        let array = JObjectArray::from(value);
        let len = env.get_array_length(&array).map_err(|e| format!("listPorts len: {e}"))?;
        let mut out = Vec::with_capacity(len as usize);
        for i in 0..len {
            let elem = env
                .get_object_array_element(&array, i)
                .map_err(|e| format!("listPorts[{i}]: {e}"))?;
            out.push(jstring_to_rust(env, &elem)?);
        }
        Ok(out)
    })
}

/// Opens the Captain's data CDC interface. `baud` is accepted for call-site
/// symmetry with the old plugin-based `open()` but is otherwise unused -
/// CircuitPython's CDC ACM does not act on the line-coding baud value (see
/// BosunSerialDevice.setLineCoding's doc comment). Returns the canonical
/// port name on success.
pub fn open(path: String, _baud: u32, generation: u64) -> Result<String, String> {
    with_jni(move |env, activity| {
        let context = application_context(env, activity)?;
        let port_js = env.new_string(&path).map_err(|e| format!("new_string: {e}"))?;
        let class = bridge_class(env, activity)?;
        let value = env
            .call_static_method(
                class, "open", OPEN_SIG,
                &[JValue::Object(&context), JValue::Object(&port_js), JValue::Long(generation as i64)],
            )
            .and_then(|v| v.l());
        take_pending_exception(env)?;
        let value = value.map_err(|e| format!("open: {e}"))?;
        jstring_to_rust(env, &value)
    })
}

pub fn close(_path: String, generation: u64) -> Result<(), String> {
    with_jni(move |env, activity| {
        let context = application_context(env, activity)?;
        let class = bridge_class(env, activity)?;
        let result = env.call_static_method(class, "close", CLOSE_SIG, &[JValue::Object(&context), JValue::Long(generation as i64)]);
        take_pending_exception(env)?;
        result.map_err(|e| format!("close: {e}"))?;
        Ok(())
    })
}

/// Reads up to `max_len` bytes, blocking at most `timeout_ms`. An empty
/// string means a normal timeout with nothing available (matches the old
/// plugin path's contract, which `android.rs`'s `Ok(data) if
/// !data.is_empty()` check already expects).
pub fn read(timeout_ms: u64, max_len: usize, generation: u64) -> Result<String, String> {
    with_jni(move |env, _activity| {
        let class = bridge_class(env, _activity)?;
        let value = env
            .call_static_method(
                class, "read", READ_SIG,
                &[JValue::Long(generation as i64), JValue::Int(max_len as i32), JValue::Int(timeout_ms as i32)],
            )
            .and_then(|v| v.l());
        take_pending_exception(env)?;
        let value = value.map_err(|e| format!("read: {e}"))?;
        let array = JByteArray::from(value);
        let bytes = env.convert_byte_array(&array).map_err(|e| format!("read decode: {e}"))?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    })
}

/// Writes `data`, blocking at most `timeout_ms`. Returns the byte count
/// written.
pub fn write(data: String, timeout_ms: u64, generation: u64) -> Result<usize, String> {
    with_jni(move |env, _activity| {
        let class = bridge_class(env, _activity)?;
        let bytes = data.as_bytes();
        let array = env.byte_array_from_slice(bytes).map_err(|e| format!("write encode: {e}"))?;
        let value = env.call_static_method(
            class, "write", WRITE_SIG,
            &[JValue::Long(generation as i64), JValue::Object(&array), JValue::Int(timeout_ms as i32)],
        );
        take_pending_exception(env)?;
        let n = value.and_then(|v| v.i()).map_err(|e| format!("write: {e}"))?;
        Ok(n as usize)
    })
}
