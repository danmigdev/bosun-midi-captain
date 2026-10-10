//! Serial-over-USB-CDC communication with the pedal.
//!
//! Platform-specific backends: `desktop.rs` (serial2, Windows/macOS/Linux)
//! and `android.rs` (raw USB via `android_native.rs`'s JNI bridge to the
//! Kotlin `BosunSerialBridge`).
//!
//! Only one of the two submodules is compiled at a time, controlled by
//! `#[cfg(target_os = "android")]`. This module re-exports whichever is
//! active so `main.rs` can use `serial::*` uniformly.
//!
//! `android_helpers.rs` is pure logic for the Android backend (desktop also
//! uses its sync marker check), compiled on every target so its regression
//! tests run on the host.

mod android_helpers;

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
