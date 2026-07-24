#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// Desktop entry point. Calls into lib.rs which contains the full Tauri
// app setup. On Android, lib.rs is compiled as a cdylib and the
// #[cfg_attr(mobile, tauri::mobile_entry_point)] macro generates the
// JNI entry point automatically.

fn main() {
    bosun_editor_lib::run();
}
