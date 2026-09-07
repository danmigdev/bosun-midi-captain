fn main() {
    // Native release assets are optional in source/CI builds. The packager
    // populates this directory after auditing the RP2040 image.
    std::fs::create_dir_all("resources/update").expect("create optional update resources");
    tauri_build::build()
}
