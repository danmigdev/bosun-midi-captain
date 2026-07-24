// Bosun editor - shared library entry point.
// Used by the desktop binary (main.rs) and the Android shared library.
// The #[cfg_attr(mobile, tauri::mobile_entry_point)] attribute generates
// the JNI entry point that Android needs to load the Rust code.

mod export;
mod midi;
mod serial;

#[cfg(not(target_os = "android"))]
mod installer;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(serial::AppState::default());

    // Window state persistence is desktop-only (no window sizing on Android).
    #[cfg(not(target_os = "android"))]
    {
        builder = builder.plugin(tauri_plugin_window_state::Builder::default().build());
    }

    // Serial backend: serial2 on desktop, tauri-plugin-serialplugin on Android.
    #[cfg(target_os = "android")]
    {
        builder = builder.plugin(tauri_plugin_serialplugin::init());
    }

    // MIDI bridge: desktop-only (uses midir with winmm/CoreMIDI/ALSA).
    #[cfg(not(target_os = "android"))]
    {
        builder = builder.manage(midi::MidiState::default());
    }

    // ---- command handlers ----
    // Each platform registers its own set of commands via a separate cfg block
    // so generate_handler!'s inferred type stays within a single scope.

    #[cfg(not(target_os = "android"))]
    {
        builder = builder.invoke_handler(tauri::generate_handler![
            serial::list_ports,
            serial::connect,
            serial::auto_connect,
            serial::disconnect,
            serial::send_command,
            serial::is_connected,
            serial::drain_inbox,
            serial::reboot_to_bootloader,
            installer::detect_pedal,
            installer::flash_circuitpython,
            installer::install_firmware,
            installer::list_firmware_files,
            installer::read_firmware_file_b64,
            installer::bundled_firmware_version,
            installer::pick_firmware_source,
            installer::prepare_firmware_source,
            installer::list_firmware_files_at,
            installer::read_firmware_file_at_b64,
            export::pick_export_folder,
            export::write_export_file,
            export::default_backup_folder,
            export::open_in_file_manager,
            midi::midi_list_ports,
            midi::midi_bridge_start,
            midi::midi_bridge_stop,
            midi::midi_bridge_status,
        ]);
    }

    #[cfg(target_os = "android")]
    {
        builder = builder.invoke_handler(tauri::generate_handler![
            serial::list_ports,
            serial::connect,
            serial::auto_connect,
            serial::disconnect,
            serial::send_command,
            serial::is_connected,
            serial::drain_inbox,
            export::pick_export_folder,
            export::write_export_file,
            export::default_backup_folder,
            export::open_in_file_manager,
        ]);
    }

    builder
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
