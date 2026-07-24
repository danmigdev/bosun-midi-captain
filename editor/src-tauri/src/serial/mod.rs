//! Serial-over-USB-CDC communication with the pedal.
//!
//! Platform-specific backends: `desktop.rs` (serial2, Windows/macOS/Linux)
//! and `android.rs` (tauri-plugin-serialplugin).
//!
//! Only one of the two submodules is compiled at a time, controlled by
//! `#[cfg(target_os = "android")]`. This module re-exports whichever is
//! active so `main.rs` can use `serial::*` uniformly.

#[cfg(not(target_os = "android"))]
mod desktop;
#[cfg(not(target_os = "android"))]
pub use desktop::*;

#[cfg(target_os = "android")]
mod android;
#[cfg(target_os = "android")]
pub use android::*;
