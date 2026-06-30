#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod export;
mod installer;
mod midi;
mod serial;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(serial::AppState::default())
        .manage(midi::MidiState::default())
        .invoke_handler(tauri::generate_handler![
            serial::list_ports,
            serial::connect,
            serial::auto_connect,
            serial::disconnect,
            serial::send_command,
            serial::is_connected,
            serial::drain_inbox,
            serial::reboot_to_bootloader,
            installer::debug_log,
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
