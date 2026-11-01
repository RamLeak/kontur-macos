//! The complete frontend command surface. Paths and executable names never arrive over IPC.
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::{atomic::{AtomicBool, AtomicUsize, Ordering}, Arc, Mutex},
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use tauri::{Manager, State, WebviewWindow};

use crate::{
    codex::{CodexClosePermit, CodexError, CodexPoll, CodexRequest, CodexService, CodexStatus, LoginState, StatusState},
    domain::*, platform, storage::Store,
};

// The measured bank already exceeds 2.5 GB by asset ID. Full transfers stream
// their bytes; legacy mini-store commands retain the original 256-MiB bound.
const MAX_FULL_CONTAINER_BYTES: u64 = 3 * 1024 * 1024 * 1024 + 512 * 1024 * 1024;

pub(crate) struct AppState(pub Arc<NativeState>);

pub(crate) struct NativeState {
    pub store: Mutex<Store>,
    pub codex: Arc<CodexService>,
    pub backend: Option<Arc<crate::backend::BackendService>>,
    pub root: PathBuf,
    pub inflight_writes: AtomicUsize,
    pub snapshot_loaded: AtomicBool,
    pub dialog_open: AtomicBool,
    pub ai_close_unavailable: AtomicBool,
    pub close: Mutex<CloseState>,
    import: Mutex<Option<ImportLease>>,
    full_import: Mutex<Option<FullImportLease>>,
}

#[derive(Default)]
pub(crate) struct CloseState {
    pub nonce: Option<String>,
    pub sealed: bool,
    pub accepted: bool,
}

struct ImportLease {
    token: String,
    path: PathBuf,
    checksum: String,
    created: Instant,
}

struct FullImportLease { token: String, path: PathBuf, checksum: String, created: Instant }
impl Drop for FullImportLease {
    fn drop(&mut self) {
        // This type is constructed only for a create_new file in import-staging.
        let _ = std::fs::remove_file(&self.path);
    }
}

impl NativeState {
    pub fn new(root: PathBuf, store: Store) -> Self {
        Self {
            codex: Arc::new(CodexService::new(root.clone())), backend: None, root, store: Mutex::new(store),
            inflight_writes: AtomicUsize::new(0), snapshot_loaded: AtomicBool::new(false),
            dialog_open: AtomicBool::new(false), close: Mutex::new(CloseState::default()),
            ai_close_unavailable: AtomicBool::new(false),
            import: Mutex::new(None),
            full_import: Mutex::new(None),
        }
    }
}

fn unavailable() -> AppError {
    AppError::new("native_unavailable", "Локальное приложение временно недоступно. Повторите действие.")
}
fn close_error() -> AppError {
    AppError::new("close_not_saved", "Сохранение не подтверждено. Окно осталось открытым.")
}
fn close_ai_error() -> AppError {
    AppError::new("close_ai_unavailable", "Окно осталось открытым. ИИ недоступен до перезапуска приложения; документы доступны.")
}

struct WritePermit(Arc<NativeState>);
impl WritePermit {
    fn begin(core: &Arc<NativeState>) -> AppResult<Self> {
        let close = core.close.lock().map_err(|_| unavailable())?;
        if close.sealed { return Err(close_error()); }
        // Count before dispatching a blocking operation, including queued writers.
        core.inflight_writes.fetch_add(1, Ordering::SeqCst);
        drop(close);
        Ok(Self(core.clone()))
    }
}
impl Drop for WritePermit {
    fn drop(&mut self) { self.0.inflight_writes.fetch_sub(1, Ordering::SeqCst); }
}

async fn store_call<T: Send + 'static>(core: Arc<NativeState>, writes: bool,
    action: impl FnOnce(&mut Store) -> AppResult<T> + Send + 'static) -> AppResult<T> {
    let permit = if writes { Some(WritePermit::begin(&core)?) } else { None };
    tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        let mut store = core.store.lock().map_err(|_| unavailable())?;
        action(&mut store)
    }).await.map_err(|_| unavailable())?
}

#[tauri::command]
pub(crate) async fn local_api_request(state: State<'_, AppState>, window: WebviewWindow,
    request: crate::backend::ApiRequest) -> AppResult<crate::backend::ApiResponse> {
    if window.label() != "main" || !crate::bundled_navigation(&window.url().map_err(|_| unavailable())?) {
        return Err(unavailable());
    }
    request.validate()?;
    let core = state.inner().0.clone();
    let backend = core.backend.clone().ok_or_else(unavailable)?;
    // Original GET handlers can update lazy sessions or refresh run history.
    // Count every request and admit none after the close proof starts.
    let permit = WritePermit::begin(&core)?;
    let request_core = core.clone();
    let response = tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        let response = backend.request(request)?;
        request_core.snapshot_loaded.store(true, Ordering::Release);
        Ok(response)
    }).await.map_err(|_| unavailable())??;
    Ok(response)
}

#[tauri::command]
pub(crate) async fn load_snapshot(state: State<'_, AppState>) -> AppResult<Snapshot> {
    let core = state.inner().0.clone();
    let snapshot = store_call(core.clone(), false, |store| store.snapshot()).await?;
    core.snapshot_loaded.store(true, Ordering::Release);
    Ok(snapshot)
}

#[tauri::command]
pub(crate) async fn create_document(state: State<'_, AppState>, input: CreateDocument) -> AppResult<Document> {
    store_call(state.inner().0.clone(), true, move |store| store.create_document(input)).await
}

#[tauri::command]
pub(crate) async fn load_document(state: State<'_, AppState>, document_id: String) -> AppResult<Document> {
    store_call(state.inner().0.clone(), true, move |store| store.document(&document_id)).await
}

#[tauri::command]
pub(crate) async fn save_document(state: State<'_, AppState>, input: SaveDocument) -> AppResult<Document> {
    store_call(state.inner().0.clone(), true, move |store| store.save_document(input)).await
}

#[tauri::command]
pub(crate) async fn mark_attempt(state: State<'_, AppState>, mut input: MarkAttempt) -> AppResult<ProgressSummary> {
    input.source = AttemptSource::Manual;
    // The native Store assigns its own current time for a new event and reuses
    // its committed timestamp on an identical stable-ID retry. Ignore UI time.
    input.created_at = None;
    store_call(state.inner().0.clone(), true, move |store| store.mark_attempt(input)).await
}

#[tauri::command]
pub(crate) async fn load_asset(state: State<'_, AppState>, asset_id: String) -> AppResult<AssetData> {
    store_call(state.inner().0.clone(), false, move |store| store.asset(&asset_id)).await
}

struct DialogPermit(Arc<NativeState>);
impl Drop for DialogPermit {
    fn drop(&mut self) { self.0.dialog_open.store(false, Ordering::Release); }
}

async fn choose_path(core: Arc<NativeState>, window: WebviewWindow,
    choose: impl FnOnce(isize) -> AppResult<Option<PathBuf>> + Send + 'static) -> AppResult<Option<PathBuf>> {
    {
        let close = core.close.lock().map_err(|_| unavailable())?;
        if close.sealed || close.nonce.is_some() || core.dialog_open.swap(true, Ordering::AcqRel) {
            return Err(unavailable());
        }
    }
    let permit = DialogPermit(core.clone());
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    let owner = window.hwnd().map_err(|_| unavailable())?.0 as isize;
    let app = window.app_handle().clone();
    app.run_on_main_thread(move || { let _ = sender.send(choose(owner)); }).map_err(|_| unavailable())?;
    tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        receiver.recv().map_err(|_| unavailable())?
    }).await.map_err(|_| unavailable())?
}

fn protect_local_state(path: &Path, root: &Path) -> AppResult<()> {
    // The native dialog may choose any ordinary user file, except the live app's
    // state and WebView profile. Canonicalize the parent to catch junctions.
    let parent = path.parent().and_then(|value| value.canonicalize().ok()).ok_or_else(unavailable)?;
    let root = root.canonicalize().map_err(|_| unavailable())?;
    let parent = parent.to_string_lossy().to_lowercase();
    let root = root.to_string_lossy().to_lowercase();
    if parent == root || parent.starts_with(&(root + "\\")) {
        return Err(AppError::new("invalid_path", "Сохраните файл вне служебной папки приложения."));
    }
    Ok(())
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ImportSelection { token: String, preview: ImportPreview }

#[tauri::command]
pub(crate) async fn preview_import(state: State<'_, AppState>, window: WebviewWindow) -> AppResult<Option<ImportSelection>> {
    let core = state.inner().0.clone();
    // Starting a new selection invalidates the previous preview, including when
    // the user cancels the OS dialog. Never retain a lock after preview returns.
    core.import.lock().map_err(|_| unavailable())?.take();
    let Some(path) = choose_path(core.clone(), window, platform::open_bundle).await? else { return Ok(None); };
    let token = platform::random_token()?;
    let lease_path = path.clone();
    let preview = store_call(core.clone(), false, move |store| {
        use std::os::windows::fs::OpenOptionsExt;
        // Prevent mutation only during this preview read. Confirm uses storage's
        // bounded private copy and SHA binding rather than a long-lived handle.
        let lock = std::fs::OpenOptions::new().read(true).share_mode(1).open(&path)
            .map_err(|_| crate::domain::io_error())?;
        let metadata = lock.metadata().map_err(|_| crate::domain::io_error())?;
        if !metadata.is_file() || metadata.len() > 256 * 1024 * 1024 {
            return Err(AppError::new("invalid_bundle", "Файл данных превышает допустимый размер или недоступен."));
        }
        let preview = store.preview_import(&path)?;
        drop(lock);
        Ok(preview)
    }).await?;
    *core.import.lock().map_err(|_| unavailable())? = Some(ImportLease {
        token: token.clone(), path: lease_path, checksum: preview.content_sha256.clone(),
        created: Instant::now(),
    });
    Ok(Some(ImportSelection { token, preview }))
}

#[tauri::command]
pub(crate) async fn confirm_import(state: State<'_, AppState>, token: String) -> AppResult<ImportResult> {
    let core = state.inner().0.clone();
    let lease = {
        let mut pending = core.import.lock().map_err(|_| unavailable())?;
        consume_import_token(&mut pending, &token)?
    };
    store_call(core, true, move |store| {
        let result = store.import_bundle_checked(&lease.path, &lease.checksum);
        drop(lease);
        result
    }).await
}

fn consume_import_token(pending: &mut Option<ImportLease>, token: &str) -> AppResult<ImportLease> {
    if pending.as_ref().is_some_and(|value| value.created.elapsed() >= Duration::from_secs(300)) {
        pending.take();
    }
    if !pending.as_ref().is_some_and(|value| value.token == token) {
        return Err(AppError::new("invalid_import_token", "Выберите файл данных заново."));
    }
    pending.take().ok_or_else(unavailable)
}

#[tauri::command]
pub(crate) async fn export_bundle(state: State<'_, AppState>, window: WebviewWindow) -> AppResult<Option<ExportResult>> {
    let core = state.inner().0.clone();
    let Some(path) = choose_path(core.clone(), window, platform::save_bundle).await? else { return Ok(None); };
    protect_local_state(&path, &core.root)?;
    store_call(core, true, move |store| store.export_bundle(&path).map(Some)).await
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FullImportSelection { token: String, preview: serde_json::Value }

fn bundled_window(window: &WebviewWindow) -> AppResult<()> {
    if window.label() == "main" && crate::bundled_navigation(&window.url().map_err(|_| unavailable())?) { Ok(()) }
    else { Err(unavailable()) }
}

#[tauri::command]
pub(crate) async fn local_import_preview(state: State<'_, AppState>, window: WebviewWindow)
    -> AppResult<Option<FullImportSelection>> {
    bundled_window(&window)?;
    let core = state.inner().0.clone();
    core.full_import.lock().map_err(|_| unavailable())?.take();
    let Some(path) = choose_path(core.clone(), window, platform::open_bundle).await? else { return Ok(None); };
    let token = platform::random_token()?;
    let backend = core.backend.clone().ok_or_else(unavailable)?;
    let permit = WritePermit::begin(&core)?;
    let worker_core = core.clone();
    let (lease, preview) = tauri::async_runtime::spawn_blocking(move || -> AppResult<_> {
        use std::{io::{Read, Write}, os::windows::fs::OpenOptionsExt};
        let _permit = permit;
        let source = std::fs::OpenOptions::new().read(true).share_mode(1).open(&path).map_err(|_| io_error())?;
        let metadata = source.metadata().map_err(|_| io_error())?;
        if !metadata.is_file() || metadata.len() > MAX_FULL_CONTAINER_BYTES { return Err(invalid_data()); }
        let stage = worker_core.root.join("import-staging");
        std::fs::create_dir_all(&stage).map_err(|_| io_error())?;
        let staged_path = stage.join(format!("{}.kontur", platform::random_token()?));
        let mut lease = FullImportLease { token, path: staged_path, checksum: String::new(), created: Instant::now() };
        let mut output = std::fs::OpenOptions::new().write(true).create_new(true).open(&lease.path).map_err(|_| io_error())?;
        let count = std::io::copy(&mut source.take(MAX_FULL_CONTAINER_BYTES + 1), &mut output).map_err(|_| io_error())?;
        if count != metadata.len() || count > MAX_FULL_CONTAINER_BYTES { return Err(invalid_data()); }
        output.flush().map_err(|_| io_error())?; output.sync_all().map_err(|_| io_error())?; drop(output);
        lease.checksum = platform::sha256_file(&lease.path, MAX_FULL_CONTAINER_BYTES)?;
        let preview = backend.call("import_preview", serde_json::json!({"path":lease.path}))?;
        Ok((lease, preview))
    }).await.map_err(|_| unavailable())??;
    let token = lease.token.clone();
    *core.full_import.lock().map_err(|_| unavailable())? = Some(lease);
    Ok(Some(FullImportSelection { token, preview }))
}

#[tauri::command]
pub(crate) async fn local_import_confirm(state: State<'_, AppState>, window: WebviewWindow, token: String)
    -> AppResult<serde_json::Value> {
    bundled_window(&window)?;
    let core = state.inner().0.clone();
    let lease = {
        let mut pending = core.full_import.lock().map_err(|_| unavailable())?;
        if pending.as_ref().is_some_and(|p| p.created.elapsed() >= Duration::from_secs(300)) { pending.take(); }
        if !pending.as_ref().is_some_and(|p| p.token == token) {
            return Err(AppError::new("invalid_import_token", "Выберите файл данных заново."));
        }
        pending.take().ok_or_else(unavailable)?
    };
    let backend = core.backend.clone().ok_or_else(unavailable)?;
    let permit = WritePermit::begin(&core)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        if platform::sha256_file(&lease.path, MAX_FULL_CONTAINER_BYTES)? != lease.checksum {
            return Err(invalid_data());
        }
        backend.call("import_bundle", serde_json::json!({"path":lease.path,"expectedSha256":lease.checksum}))
    }).await.map_err(|_| unavailable())?
}

#[tauri::command]
pub(crate) async fn local_export_bundle(state: State<'_, AppState>, window: WebviewWindow)
    -> AppResult<Option<serde_json::Value>> {
    bundled_window(&window)?;
    let core = state.inner().0.clone();
    let Some(path) = choose_path(core.clone(), window, platform::save_bundle).await? else { return Ok(None); };
    protect_local_state(&path, &core.root)?;
    let backend = core.backend.clone().ok_or_else(unavailable)?;
    let permit = WritePermit::begin(&core)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        backend.call("export_bundle", serde_json::json!({"path":path})).map(Some)
    }).await.map_err(|_| unavailable())?
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct SavedAsset { file_name: String }

#[tauri::command]
pub(crate) async fn save_asset(state: State<'_, AppState>, window: WebviewWindow, asset_id: String) -> AppResult<Option<SavedAsset>> {
    let core = state.inner().0.clone();
    let asset = store_call(core.clone(), false, move |store| store.asset(&asset_id)).await?;
    let suggested = asset.metadata.name.clone();
    let Some(path) = choose_path(core.clone(), window, move |owner| platform::save_attachment(owner, &suggested)).await? else { return Ok(None); };
    protect_local_state(&path, &core.root)?;
    // Asset limit is enforced by storage; native adds an independent bounded
    // guard before writing, and writes in fixed chunks without invoking a shell.
    if asset.bytes.len() > 16 * 1024 * 1024 { return Err(crate::domain::invalid_data()); }
    let permit = WritePermit::begin(&core)?;
    tauri::async_runtime::spawn_blocking(move || {
        let _permit = permit;
        platform::write_selected_file(&path, &asset.bytes)?;
        Ok(Some(SavedAsset { file_name: path.file_name().unwrap_or_default().to_string_lossy().into_owned() }))
    }).await.map_err(|_| unavailable())?
}

#[tauri::command]
pub(crate) fn codex_status(state: State<'_, AppState>) -> CodexStatus {
    let closing = state.inner().0.close.lock();
    let ai_unavailable = state.inner().0.ai_close_unavailable.load(Ordering::Acquire);
    if ai_unavailable || closing.as_ref().map(|value| value.nonce.is_some() || value.sealed).unwrap_or(true) {
        // A late UI status poll must not create a new probe after shutdown.
        return CodexStatus { state: StatusState::Unavailable, cli_version: None,
            gateway_protocol: 1,
            login_state: LoginState::Unknown, manual_model_supported: true,
            manual_models: Vec::new(), active_request_id: None,
            error: Some(if ai_unavailable { codex_close_unavailable() } else { codex_closing() }) };
    }
    state.inner().0.codex.status()
}
#[tauri::command]
pub(crate) fn codex_start(state: State<'_, AppState>, request: CodexRequest) -> Result<CodexPoll, CodexError> {
    let close = state.inner().0.close.lock().map_err(|_| codex_closing())?;
    if state.inner().0.ai_close_unavailable.load(Ordering::Acquire) { return Err(codex_close_unavailable()); }
    if close.nonce.is_some() || close.sealed { return Err(codex_closing()); }
    state.inner().0.codex.start(request)
}
#[tauri::command]
pub(crate) fn codex_poll(state: State<'_, AppState>, request_id: String) -> Result<CodexPoll, CodexError> {
    state.inner().0.codex.poll(&request_id)
}
#[tauri::command]
pub(crate) fn codex_cancel(state: State<'_, AppState>, request_id: String) -> Result<CodexPoll, CodexError> {
    state.inner().0.codex.cancel(&request_id)
}
#[tauri::command]
pub(crate) fn codex_login(state: State<'_, AppState>) -> Result<CodexStatus, CodexError> {
    let close = state.inner().0.close.lock().map_err(|_| codex_closing())?;
    if state.inner().0.ai_close_unavailable.load(Ordering::Acquire) { return Err(codex_close_unavailable()); }
    if close.nonce.is_some() || close.sealed { return Err(codex_closing()); }
    state.inner().0.codex.login()
}

fn codex_closing() -> CodexError {
    CodexError { code: "codex_busy".into(), message: "Дождитесь завершения сохранения и закрытия окна.".into() }
}
fn codex_close_unavailable() -> CodexError {
    CodexError { code: "codex_cancel_failed".into(), message: "ИИ недоступен до перезапуска приложения. Локальные документы доступны.".into() }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct SavedDocument { document_id: String, revision: i64 }

fn seal_close(core: &NativeState, nonce: &str, saved_documents: &[SavedDocument], all_flushed: bool) -> AppResult<()> {
    if let Some(backend) = &core.backend {
        let mut close = core.close.lock().map_err(|_| unavailable())?;
        if !all_flushed || close.nonce.as_deref() != Some(nonce) || close.accepted || close.sealed
            || core.inflight_writes.load(Ordering::SeqCst) != 0 || core.dialog_open.load(Ordering::Acquire) {
            return Err(close_error());
        }
        close.sealed = true;
        drop(close);
        // The frontend flushes its editors; the private backend then drains its
        // actual mutations and confirms a durable SQLite checkpoint. The sealed
        // gate rejects new API calls without holding a mutex across child I/O.
        let result = backend.call("flush", serde_json::json!({})).and_then(|proof| {
            if proof.get("committed").and_then(serde_json::Value::as_bool) != Some(true)
                || proof.get("pendingMutations").and_then(serde_json::Value::as_u64) != Some(0) {
                Err(close_error())
            } else { Ok(()) }
        });
        if result.is_err() {
            let mut close = core.close.lock().map_err(|_| unavailable())?;
            if close.nonce.as_deref() == Some(nonce) && !close.accepted { close.sealed = false; }
        }
        return result;
    }
    let store = core.store.lock().map_err(|_| unavailable())?;
    let mut close = core.close.lock().map_err(|_| unavailable())?;
    if !all_flushed || close.nonce.as_deref() != Some(nonce) || close.accepted || close.sealed
        || core.inflight_writes.load(Ordering::SeqCst) != 0
        || core.dialog_open.load(Ordering::Acquire) || saved_documents.len() > 10_000 {
        return Err(close_error());
    }
    let mut ids = HashSet::new();
    for document in saved_documents {
        if document.document_id.is_empty() || document.document_id.len() > 256 || document.revision < 1
            || !ids.insert(document.document_id.as_str()) { return Err(close_error()); }
        store.verify_document_revision(&document.document_id, document.revision)?;
    }
    let snapshot = store.snapshot()?;
    if !core.snapshot_loaded.load(Ordering::Acquire) && !snapshot.documents.is_empty() { return Err(close_error()); }
    if let Some(selected) = &snapshot.preferences.selected_document_id {
        if core.snapshot_loaded.load(Ordering::Acquire) && !ids.contains(selected.as_str()) { return Err(close_error()); }
    }
    // With the store and close locks held, a new write cannot pass begin().
    close.sealed = true;
    Ok(())
}

#[tauri::command]
pub(crate) async fn confirm_close(state: State<'_, AppState>, window: WebviewWindow,
    nonce: String, saved_documents: Vec<SavedDocument>, all_flushed: bool) -> AppResult<()> {
    let core = state.inner().0.clone();
    let closing = core.clone();
    let checked_nonce = nonce.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        seal_close(&closing, &checked_nonce, &saved_documents, all_flushed)
    }).await.unwrap_or_else(|_| Err(unavailable()));
    if let Err(error) = result {
        recover_close(&window, &core, &nonce);
        return Err(error);
    }
    complete_sealed_close(window, core, nonce).await
}

async fn complete_sealed_close(window: WebviewWindow, core: Arc<NativeState>, nonce: String) -> AppResult<()> {
    let placement_window = window.clone();
    let placement_root = core.root.clone();
    let placement = tauri::async_runtime::spawn_blocking(move ||
        crate::persist_window_state(&placement_window, &placement_root))
        .await.unwrap_or_else(|_| Err(unavailable()));
    if let Err(error) = placement {
        recover_close_internal(&window, &core, &nonce, true, None, false);
        return Err(error);
    }
    let shutdown_core = core.clone();
    let permit = match tauri::async_runtime::spawn_blocking(move || shutdown_core.codex.shutdown()).await {
        Ok(Ok(permit)) => permit,
        _ => {
            recover_close_internal(&window, &core, &nonce, true, None, true);
            return Err(close_ai_error());
        }
    };
    {
        let mut close = core.close.lock().map_err(|_| unavailable())?;
        if close.nonce.as_deref() != Some(&nonce) || !close.sealed || close.accepted { return Err(close_error()); }
        close.accepted = true;
    }
    if window.close().is_err() {
        if let Ok(mut close) = core.close.lock() {
            if close.nonce.as_deref() == Some(&nonce) { close.accepted = false; }
        }
        recover_close_internal(&window, &core, &nonce, true, Some(&permit), false);
        return Err(if core.ai_close_unavailable.load(Ordering::Acquire) { close_ai_error() } else { unavailable() });
    }
    Ok(())
}

pub(crate) fn recover_close(window: &WebviewWindow, core: &Arc<NativeState>, nonce: &str) {
    // Timeout/hook/duplicate-ACK failures never unseal a completion that already
    // owns the native critical phase and its Codex close receipt.
    recover_close_internal(window, core, nonce, false, None, false);
}

fn recover_close_internal(window: &WebviewWindow, core: &Arc<NativeState>, nonce: &str,
    allow_sealed: bool, permit: Option<&CodexClosePermit>, poison_ai: bool) {
    let recovered = if let Ok(mut close) = core.close.lock() {
        if close.nonce.as_deref() == Some(nonce) && !close.accepted && (allow_sealed || !close.sealed) {
            // Keep the fresh native challenge and sealed gate locked until a
            // generation-bound rearm succeeds or AI is explicitly unavailable.
            if poison_ai || permit.is_some_and(|value| core.codex.resume_after_aborted_close(value).is_err()) {
                core.ai_close_unavailable.store(true, Ordering::Release);
            }
            close.nonce = None; close.sealed = false; true
        } else { false }
    } else { false };
    if !recovered { return; }
    let _ = window.set_enabled(true);
    let window = window.clone();
    let core = core.clone();
    let app = window.app_handle().clone();
    let _ = app.run_on_main_thread(move || {
        let owner = window.hwnd().map(|value| value.0 as isize).unwrap_or(0);
        if platform::close_retry(owner, core.ai_close_unavailable.load(Ordering::Acquire)) { request_close(&window, &core); }
    });
}

pub(crate) fn request_close(window: &WebviewWindow, core: &Arc<NativeState>) {
    if core.dialog_open.load(Ordering::Acquire) { return; }
    let Ok(nonce) = platform::random_token() else { return; };
    {
        let Ok(mut close) = core.close.lock() else { return; };
        if close.nonce.is_some() || close.accepted { return; }
        if window.set_enabled(false).is_err() { return; }
        close.nonce = Some(nonce.clone());
    }
    let timeout_window = window.clone();
    let timeout_core = core.clone();
    let timeout_nonce = nonce.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_secs(20));
        recover_close(&timeout_window, &timeout_core, &timeout_nonce);
    });
    if core.backend.is_some() && !core.snapshot_loaded.load(Ordering::Acquire) {
        let window = window.clone();
        let core = core.clone();
        tauri::async_runtime::spawn(async move {
            let proof_core = core.clone();
            let proof_nonce = nonce.clone();
            let proof = tauri::async_runtime::spawn_blocking(move || -> AppResult<bool> {
                let mut close = proof_core.close.lock().map_err(|_| unavailable())?;
                if close.nonce.as_deref() != Some(&proof_nonce) || close.sealed { return Err(close_error()); }
                if proof_core.snapshot_loaded.load(Ordering::Acquire)
                    || proof_core.inflight_writes.load(Ordering::SeqCst) != 0 { return Ok(false); }
                close.sealed = true;
                drop(close);
                // No response has reached the frontend, so no editor can have
                // unsaved changes. Cancel startup before acknowledging close.
                proof_core.backend.as_ref().ok_or_else(unavailable)?.shutdown()?;
                Ok(true)
            }).await.unwrap_or_else(|_| Err(unavailable()));
            match proof {
                Ok(true) => { let _ = complete_sealed_close(window, core, nonce).await; }
                Ok(false) => dispatch_close_request(&window, &core, &nonce),
                Err(_) => recover_close(&window, &core, &nonce),
            }
        });
        return;
    }
    if core.backend.is_none() && !core.snapshot_loaded.load(Ordering::Acquire) {
        let window = window.clone();
        let core = core.clone();
        tauri::async_runtime::spawn(async move {
            // A not-yet-loaded window can close without JS only when native
            // storage proves that there is no editable document or queued write.
            let proof_core = core.clone();
            let proof_nonce = nonce.clone();
            let proof = tauri::async_runtime::spawn_blocking(move || -> AppResult<bool> {
                let store = proof_core.store.lock().map_err(|_| unavailable())?;
                let mut close = proof_core.close.lock().map_err(|_| unavailable())?;
                if close.nonce.as_deref() != Some(&proof_nonce) || close.sealed { return Err(close_error()); }
                if proof_core.snapshot_loaded.load(Ordering::Acquire) || proof_core.inflight_writes.load(Ordering::SeqCst) != 0
                    || !store.snapshot()?.documents.is_empty() { return Ok(false); }
                close.sealed = true;
                Ok(true)
            }).await.unwrap_or_else(|_| Err(unavailable()));
            match proof {
                Ok(true) => { let _ = complete_sealed_close(window, core, nonce).await; }
                Ok(false) => dispatch_close_request(&window, &core, &nonce),
                Err(_) => recover_close(&window, &core, &nonce),
            }
        });
    } else { dispatch_close_request(window, core, &nonce); }
}

fn dispatch_close_request(window: &WebviewWindow, core: &Arc<NativeState>, nonce: &str) {
    let encoded = match serde_json::to_string(&nonce) { Ok(value) => value, Err(_) => { recover_close(window, core, &nonce); return; } };
    let script = format!("(() => {{ const nonce={encoded}; const failed=()=>window.__TAURI__.core.invoke('confirm_close',{{nonce,savedDocuments:[],allFlushed:false}}).catch(()=>{{}}); try {{ const fn=window.__KONTUR_LOCAL_REQUEST_CLOSE__; if(typeof fn!=='function') return failed(); Promise.resolve(fn({{nonce}})).catch(failed); }} catch(_) {{ failed(); }} }})()");
    if window.eval(&script).is_err() { recover_close(window, core, &nonce); return; }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture { core: Option<Arc<NativeState>>, root: PathBuf }
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("kontur-close-test-{}", platform::random_token().unwrap()));
            let store = Store::open(root.join("data")).unwrap();
            let core = Arc::new(NativeState::new(root.clone(), store));
            core.close.lock().unwrap().nonce = Some("fresh-native-nonce".into());
            Self { core: Some(core), root }
        }
        fn core(&self) -> &Arc<NativeState> { self.core.as_ref().unwrap() }
        fn document(&self) -> Document {
            self.core().snapshot_loaded.store(true, Ordering::Release);
            self.core().store.lock().unwrap().create_document(CreateDocument::default()).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            drop(self.core.take());
            // Delete only this fresh fixture's checked absolute temp directory.
            let parent = std::env::temp_dir().canonicalize().unwrap();
            let target = self.root.canonicalize().unwrap();
            assert!(target.parent() == Some(parent.as_path()));
            assert!(target.file_name().unwrap().to_string_lossy().starts_with("kontur-close-test-"));
            std::fs::remove_dir_all(target).unwrap();
        }
    }

    #[test]
    fn close_requires_fresh_nonce_explicit_flush_and_durable_selected_revision() {
        let fixture = Fixture::new();
        let document = fixture.document();
        let saved = [SavedDocument { document_id: document.id.clone(), revision: document.revision }];
        assert!(seal_close(fixture.core(), "stale", &saved, true).is_err());
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &saved, false).is_err());
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &[], true).is_err());
        let wrong = [SavedDocument { document_id: document.id.clone(), revision: document.revision + 1 }];
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &wrong, true).is_err());
        assert!(!fixture.core().close.lock().unwrap().sealed);
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &saved, true).is_ok());
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &saved, true).is_err());
        assert!(WritePermit::begin(fixture.core()).is_err());
    }

    #[test]
    fn queued_native_write_prevents_close_and_duplicate_document_acks_are_rejected() {
        let fixture = Fixture::new();
        let document = fixture.document();
        let saved = [SavedDocument { document_id: document.id.clone(), revision: document.revision }];
        let permit = WritePermit::begin(fixture.core()).unwrap();
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &saved, true).is_err());
        drop(permit);
        let duplicate = [SavedDocument { document_id: document.id.clone(), revision: document.revision }, SavedDocument { document_id: document.id, revision: document.revision }];
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &duplicate, true).is_err());
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &saved, true).is_ok());
    }

    #[test]
    fn import_token_is_single_use_expires_and_retains_no_file_handle() {
        let make = |created| ImportLease { token: "opaque".into(), path: PathBuf::from("C:\\synthetic.kontur"), checksum: "bound-sha".into(), created };
        let mut pending = Some(make(Instant::now()));
        assert!(consume_import_token(&mut pending, "wrong").is_err());
        assert!(pending.is_some());
        assert_eq!(consume_import_token(&mut pending, "opaque").unwrap().checksum, "bound-sha");
        assert!(pending.is_none());
        assert!(consume_import_token(&mut pending, "opaque").is_err());
        pending = Some(make(Instant::now() - Duration::from_secs(301)));
        assert!(consume_import_token(&mut pending, "opaque").is_err());
        assert!(pending.is_none());
    }

    #[test]
    fn full_close_seals_api_calls_without_holding_mutex_during_checkpoint() {
        let mut fixture = Fixture::new();
        let (entered_tx, entered_rx) = std::sync::mpsc::sync_channel(1);
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(1);
        let backend = crate::backend::BackendService::synthetic(move |operation, _| {
            assert_eq!(operation, "flush"); entered_tx.send(()).unwrap();
            release_rx.recv_timeout(Duration::from_secs(2)).map_err(|_| unavailable())?;
            Ok(serde_json::json!({"committed":true,"pendingMutations":0}))
        });
        Arc::get_mut(fixture.core.as_mut().unwrap()).unwrap().backend = Some(Arc::new(backend));
        let core = fixture.core().clone();
        let worker = std::thread::spawn(move || seal_close(&core, "fresh-native-nonce", &[], true));
        entered_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        let before = Instant::now();
        assert!(WritePermit::begin(fixture.core()).is_err());
        assert!(before.elapsed() < Duration::from_millis(100));
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &[], true).is_err());
        assert!(fixture.core().close.lock().unwrap().sealed);
        release_tx.send(()).unwrap(); worker.join().unwrap().unwrap();
        assert!(fixture.core().close.lock().unwrap().sealed);
    }

    #[test]
    fn failed_full_checkpoint_reopens_admission_and_can_be_retried() {
        let mut fixture = Fixture::new(); let mut calls = 0;
        let backend = crate::backend::BackendService::synthetic(move |operation, _| {
            assert_eq!(operation, "flush"); calls += 1;
            if calls == 1 { Err(AppError::new("synthetic_flush_failure", "Synthetic checkpoint failure")) }
            else { Ok(serde_json::json!({"committed":true,"pendingMutations":0})) }
        });
        Arc::get_mut(fixture.core.as_mut().unwrap()).unwrap().backend = Some(Arc::new(backend));
        assert!(seal_close(fixture.core(), "fresh-native-nonce", &[], true).is_err());
        assert!(!fixture.core().close.lock().unwrap().sealed);
        drop(WritePermit::begin(fixture.core()).unwrap());
        seal_close(fixture.core(), "fresh-native-nonce", &[], true).unwrap();
    }
}
