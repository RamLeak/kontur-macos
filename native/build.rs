fn main() {
    // Require explicit local-only permissions for the seven application
    // commands. No remote or opaque iframe origin is granted access.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "local_api_request", "local_import_preview", "local_import_confirm",
            "local_export_bundle", "codex_status", "codex_login", "confirm_close",
        ]),
    )).expect("local command capability generation failed");
}
