// Bosun editor - shared library entry point.
// Used by the desktop binary (main.rs) and the Android shared library.
// The #[cfg_attr(mobile, tauri::mobile_entry_point)] attribute generates
// the JNI entry point that Android needs to load the Rust code.

mod export;
mod hub_discovery;
mod midi;
#[cfg(target_os = "android")]
mod midi_android;
mod serial;
mod tcp_serial;

#[cfg(not(target_os = "android"))]
mod installer;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default().manage(serial::AppState::default());

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

    // MIDI bridge state: desktop-only. The desktop backend keeps its midir
    // connections here (winmm/CoreMIDI/ALSA); Android keeps the bridge state
    // in the Kotlin BosunMidiBridge singleton instead (midi_android.rs).
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
            tcp_serial::tcp_list_ports,
            tcp_serial::tcp_connect,
            hub_discovery::discover_hubs,
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
            midi_android::midi_list_ports,
            midi_android::midi_bridge_start,
            midi_android::midi_bridge_stop,
            midi_android::midi_bridge_status,
            tcp_serial::tcp_list_ports,
            tcp_serial::tcp_connect,
            hub_discovery::discover_hubs,
        ]);
    }

    builder
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
