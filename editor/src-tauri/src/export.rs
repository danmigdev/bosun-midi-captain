// Native folder picker + file writer for the "Export config" feature.
// We use `rfd` directly rather than the Tauri dialog plugin so we don't
// have to wire a plugin through main.rs - just two commands.
//
// The JS layer asks for a destination folder once, generates a
// timestamped subfolder name client-side, and then streams individual
// (filename, content) pairs in - keeping the Rust side thin and the
// progress reporting purely Svelte-driven.

use std::path::PathBuf;

/// Open a native folder picker dialog. Desktop-only (uses `rfd`).
/// On Android, returns the app's external documents directory.
#[tauri::command]
pub fn pick_export_folder() -> Result<Option<String>, String> {
    #[cfg(not(target_os = "android"))]
    {
        let picked = rfd::FileDialog::new()
            .set_title("Choose where to save the export")
            .pick_folder();
        return Ok(picked.map(|p| p.to_string_lossy().to_string()));
    }
    #[cfg(target_os = "android")]
    {
        // On Android, use the app's external files directory as the
        // default export location (no native folder picker available).
        let docs = dirs::document_dir()
            .or_else(dirs::home_dir)
            .unwrap_or_else(|| PathBuf::from("."));
        Ok(Some(docs.to_string_lossy().to_string()))
    }
}

#[tauri::command]
pub fn write_export_file(folder: String, relative: String, content: String) -> Result<String, String> {
    let mut path = PathBuf::from(&folder);
    // Guard against path traversal: the relative path must not start
    // with `/` or contain `..`. The JS side never has user input here
    // (only generated filenames and a timestamped subfolder name), but
    // a tiny sanity check costs nothing and protects against future
    // refactors.
    for component in relative.split(['/', '\\']) {
        if component.is_empty() || component == "." || component == ".." {
            return Err(format!("illegal path component: {}", component));
        }
        path.push(component);
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, content).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

/// Default destination for auto-backups: `<user docs>/bosun-backups/<sub>`.
/// Creates the directory tree so the JS side can stream files into it
/// straight away. Falls back to the user's home directory if Documents
/// can't be resolved (e.g. unusual locale on Windows).
#[tauri::command]
pub fn default_backup_folder(sub: String) -> Result<String, String> {
    let base = dirs::document_dir()
        .or_else(dirs::home_dir)
        .ok_or_else(|| "no home/documents dir".to_string())?;
    let mut path = base;
    path.push("bosun-backups");
    if !sub.is_empty() {
        for component in sub.split(['/', '\\']) {
            if component.is_empty() || component == "." || component == ".." { continue; }
            path.push(component);
        }
    }
    std::fs::create_dir_all(&path).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
pub fn open_in_file_manager(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "android")]
    {
        // Android has no native file manager intent from Rust.
        // The export folder path is shown to the user in the UI instead.
        let _ = path;
    }
    Ok(())
}
