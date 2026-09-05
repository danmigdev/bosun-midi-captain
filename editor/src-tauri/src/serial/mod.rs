//! Serial-over-USB-CDC communication with the pedal.
//!
//! Platform-specific backends: `desktop.rs` (serial2, Windows/macOS/Linux)
//! and `android.rs` (raw USB via `android_native.rs`'s JNI bridge to the
//! Kotlin `BosunSerialBridge` - see its doc comment for why this bypasses
//! tauri-plugin-serialplugin's Android backend).
//!
//! Only one of the two submodules is compiled at a time, controlled by
//! `#[cfg(target_os = "android")]`. This module re-exports whichever is
//! active so `main.rs` can use `serial::*` uniformly.
//!
//! `android_helpers.rs` is pure logic shared with the Android backend,
//! compiled on every target so its regression tests run on the host.

mod android_helpers;
pub use android_helpers::marker_found;

#[cfg(not(target_os = "android"))]
mod desktop;
#[cfg(not(target_os = "android"))]
pub use desktop::*;

#[cfg(target_os = "android")]
mod android_native;
#[cfg(target_os = "android")]
mod android;
#[cfg(target_os = "android")]
pub use android::*;
