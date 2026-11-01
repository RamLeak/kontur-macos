pub mod codex;
pub mod domain;
pub mod platform;
pub mod storage;
pub mod transfer;

#[cfg(windows)]
mod backend;
#[cfg(windows)]
mod opencode;

#[cfg(windows)]
mod commands;

#[cfg(windows)]
use std::{path::{Path, PathBuf}, sync::{Arc, Mutex}};
#[cfg(windows)]
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

#[cfg(windows)]
#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct WindowPlacement { x: i32, y: i32, width: u32, height: u32, maximized: bool }

#[cfg(windows)]
impl Default for WindowPlacement {
    fn default() -> Self { Self { x: 0, y: 0, width: 1280, height: 840, maximized: false } }
}

#[cfg(windows)]
struct PlacementState(Mutex<WindowPlacement>);
#[cfg(windows)]
struct InstanceState { _instance: Mutex<platform::Instance> }

#[cfg(windows)]
fn read_window_state(root: &Path) -> Option<WindowPlacement> {
    let path = root.join("window-state.json");
    if std::fs::metadata(&path).ok()?.len() > 4096 { return None; }
    let placement: WindowPlacement = serde_json::from_slice(&std::fs::read(path).ok()?).ok()?;
    (placement.width >= 920 && placement.height >= 620 && placement.width <= 12000 && placement.height <= 12000)
        .then_some(placement)
}

#[cfg(windows)]
fn capture_window_state(window: &WebviewWindow) {
    let Some(state) = window.try_state::<PlacementState>() else { return; };
    let Ok(mut placement) = state.inner().0.lock() else { return; };
    let Ok(maximized) = window.is_maximized() else { return; };
    placement.maximized = maximized;
    if maximized || window.is_minimized().unwrap_or(true) { return; }
    if let (Ok(position), Ok(size)) = (window.outer_position(), window.inner_size()) {
        if size.width >= 920 && size.height >= 620 && size.width <= 12000 && size.height <= 12000 {
            placement.x = position.x; placement.y = position.y;
            placement.width = size.width; placement.height = size.height;
        }
    }
}

#[cfg(windows)]
pub(crate) fn persist_window_state(window: &WebviewWindow, root: &Path) -> domain::AppResult<()> {
    capture_window_state(window);
    let state = window.state::<PlacementState>();
    let placement = state.inner().0.lock().map_err(|_| domain::io_error())?.clone();
    let bytes = serde_json::to_vec(&placement).map_err(|_| domain::io_error())?;
    platform::write_selected_file(&root.join("window-state.json"), &bytes)
}

#[cfg(windows)]
fn restore_window_state(window: &WebviewWindow, saved: Option<&WindowPlacement>) -> tauri::Result<()> {
    if let Some(saved) = saved {
        let monitors = window.available_monitors()?;
        let visible = monitors.iter().any(|monitor| {
            let position = monitor.position();
            let size = monitor.size();
            let overlap_width = (i64::from(saved.x) + i64::from(saved.width))
                .min(i64::from(position.x) + i64::from(size.width)) - i64::from(saved.x).max(i64::from(position.x));
            let overlap_height = (i64::from(saved.y) + i64::from(saved.height))
                .min(i64::from(position.y) + i64::from(size.height)) - i64::from(saved.y).max(i64::from(position.y));
            overlap_width >= 200 && overlap_height >= 150
        });
        window.set_size(tauri::PhysicalSize::new(saved.width, saved.height))?;
        if visible { window.set_position(tauri::PhysicalPosition::new(saved.x, saved.y))?; }
        else { window.center()?; }
        if saved.maximized { window.maximize()?; }
    } else { window.center()?; }
    Ok(())
}

#[cfg(windows)]
fn bundled_navigation(url: &tauri::Url) -> bool {
    // These are Tauri's internal asset origins, not listening HTTP servers.
    ((url.scheme() == "http" || url.scheme() == "https") && url.host_str() == Some("tauri.localhost")
        && url.port().is_none() && url.username().is_empty() && url.password().is_none())
        || (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
}

#[cfg(windows)]
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    let context = tauri::generate_context!();
    let Some(mut instance) = platform::Instance::claim(&context.config().identifier)? else { return Ok(()); };
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            commands::codex_status, commands::codex_login, commands::confirm_close,
            commands::local_api_request,
            commands::local_import_preview, commands::local_import_confirm, commands::local_export_bundle,
        ])
        .setup(move |app| {
            let root: PathBuf = app.path().app_local_data_dir()?;
            std::fs::create_dir_all(&root)?;
            let store = storage::Store::open(root.join("data"))?;
            let saved = read_window_state(&root);
            app.manage(PlacementState(Mutex::new(saved.clone().unwrap_or_default())));
            let mut state = commands::NativeState::new(root.clone(), store);
            let service = backend::resource_root().and_then(|bundle| backend::BackendService::launch(
                &root.join("full"), &bundle,
                &root.join("data/kontur.sqlite3"), state.codex.clone(),
            )).unwrap_or_else(|_| backend::BackendService::unavailable());
            state.backend = Some(Arc::new(service));
            let core = Arc::new(state);
            app.manage(commands::AppState(core));
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title(app.config().product_name.as_deref().unwrap_or("Контур Локальный"))
                .decorations(true).resizable(true)
                .minimizable(true).maximizable(true).closable(true)
                .inner_size(1280.0, 840.0).min_inner_size(920.0, 620.0)
                .visible(false).devtools(false)
                .data_directory(root.join("webview"))
                .on_navigation(bundled_navigation)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .build()?;
            platform::secure_webview(&window)?;
            restore_window_state(&window, saved.as_ref())?;
            instance.listen(app.handle().clone())?;
            app.manage(InstanceState { _instance: Mutex::new(instance) });
            window.show()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "main" { return; }
            let Some(webview) = window.app_handle().get_webview_window("main") else { return; };
            match event {
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    let Some(state) = window.try_state::<commands::AppState>() else { api.prevent_close(); return; };
                    let accepted = state.inner().0.close.lock().map(|close| close.accepted).unwrap_or(false);
                    if !accepted { api.prevent_close(); commands::request_close(&webview, &state.inner().0); }
                }
                tauri::WindowEvent::Moved(_) | tauri::WindowEvent::Resized(_) => capture_window_state(&webview),
                _ => {}
            }
        })
        .run(context)?;
    Ok(())
}

#[cfg(not(windows))]
pub fn run() -> Result<(), Box<dyn std::error::Error>> {
    Err("Контур Локальный поддерживает Windows.".into())
}

pub fn show_startup_error() {
    #[cfg(windows)] platform::startup_error();
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    #[test]
    fn only_tauri_asset_origins_can_navigate() {
        for value in ["http://tauri.localhost/index.html", "tauri://localhost/index.html"] {
            assert!(bundled_navigation(&tauri::Url::parse(value).unwrap()));
        }
        for value in ["https://example.com", "http://localhost:3000", "http://tauri.localhost:3000", "file:///C:/x", "https://evil@tauri.localhost"] {
            assert!(!bundled_navigation(&tauri::Url::parse(value).unwrap()));
        }
    }
}
