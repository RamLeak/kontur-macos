//! Private, bounded stdio transport to the bundled original application backend.
//! No URL, credential, executable path or filesystem path is accepted from JS.
use std::{
    collections::BTreeMap,
    ffi::OsString,
    path::{Path, PathBuf},
    sync::{atomic::{AtomicBool, Ordering}, mpsc::{self, Receiver, SyncSender, TryRecvError}, Arc, Mutex},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{codex::{CodexRequest, CodexService}, domain::{AppError, AppResult}, platform::{ManagedProcess, ProcessSpec}};

// Genuine catalog attachments reach 50,084,031 bytes before base64 encoding.
const MAX_FRAME: usize = crate::platform::PERSISTENT_OUTPUT_LIMIT;
const MAX_RESPONSE_BYTES: usize = 64_000_000;
const MAX_RESPONSE_BASE64: usize = ((MAX_RESPONSE_BYTES + 2) / 3) * 4;
// Original device history retains up to twenty 200k output records per scope.
// Preserve those states without truncation; individual domain limits still apply.
const MAX_BODY: usize = 8 * 1024 * 1024;
const MAX_PENDING: usize = 64;

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct ApiRequest {
    pub method: String,
    pub path: String,
    pub body: Option<String>,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct ApiResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body_base64: String,
}

fn failure(code: &str) -> AppError {
    let message = match code {
        "local_backend_starting" => "Подготавливаем задания и сохранённые данные…",
        "local_request_invalid" => "Недопустимый запрос к локальному приложению.",
        "local_backend_busy" => "Приложение обрабатывает данные. Повторите действие.",
        "local_backend_timeout" => "Обработка данных не завершена. Перезапустите приложение; сохранённые данные останутся на диске.",
        "local_backend_protocol" => "Не удалось подтвердить ответ локального хранилища. Перезапустите приложение.",
        _ => "Не удалось запустить локальную базу приложения. Сохранённые данные остаются на диске.",
    };
    AppError::new(code, message)
}

impl ApiRequest {
    pub(crate) fn validate(&self) -> AppResult<()> {
        if !["GET", "POST", "PUT", "PATCH", "DELETE"].contains(&self.method.as_str())
            || !self.path.starts_with("/api/v1/") || self.path.len() > 8192
            || self.path.chars().any(|c| c.is_control() || c == '\\' || c == '#')
            || self.body.as_ref().is_some_and(|b| b.len() > MAX_BODY) {
            return Err(failure("local_request_invalid"));
        }
        if self.headers.len() > 3 || self.headers.iter().any(|(key, value)| {
            !["content-type", "idempotency-key", "x-sync-owner"].contains(&key.to_ascii_lowercase().as_str())
                || value.len() > 1024 || value.chars().any(char::is_control)
        }) { return Err(failure("local_request_invalid")); }
        let path = self.path.split('?').next().unwrap_or_default().to_ascii_lowercase();
        if path.contains("%2e") || path.contains("%2f") || path.contains("%5c")
            || path.split('/').any(|part| part == "." || part == "..") {
            return Err(failure("local_request_invalid"));
        }
        Ok(())
    }
}

type Reply = SyncSender<AppResult<Value>>;
enum Command {
    Call { operation: String, fields: Value, reply: Reply },
    Stop { reply: SyncSender<AppResult<()>> },
}
struct Pending { reply: Reply, deadline: Instant }

pub(crate) struct BackendService {
    sender: SyncSender<Command>,
    alive: Arc<AtomicBool>,
    ready: Arc<AtomicBool>,
    thread: Mutex<Option<JoinHandle<()>>>,
}

fn child_visible_path(path: &Path) -> AppResult<PathBuf> {
    use std::{os::windows::ffi::{OsStrExt, OsStringExt}, path::{Component, Prefix}};
    // Python/SQLite URL construction must not see the '?' in a Windows
    // canonical \\?\ path. Retain the exact UTF-16 name and verify the same
    // existing filesystem identity before changing its representation.
    let normalized = match path.components().next() {
        Some(Component::Prefix(prefix)) => match prefix.kind() {
            Prefix::VerbatimDisk(_) => {
                let units: Vec<_> = path.as_os_str().encode_wide().collect();
                PathBuf::from(OsString::from_wide(&units[4..]))
            }
            Prefix::VerbatimUNC(_, _) => {
                let units: Vec<_> = path.as_os_str().encode_wide().collect();
                let mut unc = vec![b'\\' as u16, b'\\' as u16];
                unc.extend_from_slice(&units[8..]);
                PathBuf::from(OsString::from_wide(&unc))
            }
            Prefix::Disk(_) | Prefix::UNC(_, _) => path.to_path_buf(),
            _ => return Err(failure("local_backend_unavailable")),
        },
        _ => return Err(failure("local_backend_unavailable")),
    };
    if !path.is_absolute() || !normalized.is_absolute()
        || normalized.canonicalize().map_err(|_| failure("local_backend_unavailable"))?
            != path.canonicalize().map_err(|_| failure("local_backend_unavailable"))? {
        return Err(failure("local_backend_unavailable"));
    }
    Ok(normalized)
}

impl BackendService {
    pub(crate) fn unavailable() -> Self {
        let (sender, _receiver) = mpsc::sync_channel(MAX_PENDING);
        Self { sender, alive: Arc::new(AtomicBool::new(false)), ready: Arc::new(AtomicBool::new(false)), thread: Mutex::new(None) }
    }
    #[cfg(test)]
    pub(crate) fn synthetic(mut handler: impl FnMut(&str, Value) -> AppResult<Value> + Send + 'static) -> Self {
        let (sender, receiver) = mpsc::sync_channel(MAX_PENDING);
        let alive = Arc::new(AtomicBool::new(true));
        let worker_alive = alive.clone();
        let worker = thread::spawn(move || {
            while let Ok(command) = receiver.recv() {
                match command {
                    Command::Call { operation, fields, reply } => { let _ = reply.send(handler(&operation, fields)); }
                    Command::Stop { reply } => { let _ = reply.send(Ok(())); break; }
                }
            }
            worker_alive.store(false, Ordering::Release);
        });
        Self { sender, alive, ready: Arc::new(AtomicBool::new(true)), thread: Mutex::new(Some(worker)) }
    }

    pub(crate) fn start(profile: &Path, bundle: &Path, legacy: &Path, codex: Arc<CodexService>) -> AppResult<Self> {
        let (result, ready_receiver) = Self::spawn(profile, bundle, legacy, codex)?;
        match ready_receiver.recv_timeout(Duration::from_secs(185)) {
            Ok(Ok(())) => Ok(result),
            _ => { let _ = result.shutdown(); Err(failure("local_backend_unavailable")) }
        }
    }

    /// Start the child without blocking the native window/event loop. Requests
    /// are rejected as starting until the child's authenticated ready frame.
    pub(crate) fn launch(profile: &Path, bundle: &Path, legacy: &Path, codex: Arc<CodexService>) -> AppResult<Self> {
        Self::spawn(profile, bundle, legacy, codex).map(|(service, _)| service)
    }

    pub(crate) fn is_ready(&self) -> bool { self.ready.load(Ordering::Acquire) }

    fn spawn(profile: &Path, bundle: &Path, legacy: &Path, codex: Arc<CodexService>)
        -> AppResult<(Self, Receiver<AppResult<()>>)> {
        let python = bundle.join("python/python.exe");
        let script = bundle.join("backend/offline_bridge.py");
        if !python.is_file() || !script.is_file() || !profile.is_absolute() || !bundle.is_absolute() {
            return Err(failure("local_backend_unavailable"));
        }
        std::fs::create_dir_all(profile).map_err(|_| failure("local_backend_unavailable"))?;
        let temporary = profile.join("temporary");
        std::fs::create_dir_all(&temporary).map_err(|_| failure("local_backend_unavailable"))?;
        let python = child_visible_path(&python)?;
        let script = child_visible_path(&script)?;
        let child_profile = child_visible_path(profile)?;
        let child_bundle = child_visible_path(bundle)?;
        let temporary = child_visible_path(&temporary)?;
        let mut env = Vec::<(OsString, OsString)>::new();
        // Do not inherit Python startup paths, project configuration or credentials.
        for name in ["SystemRoot", "WINDIR"] {
            if let Some(value) = std::env::var_os(name) { env.push((name.into(), value)); }
        }
        env.push(("TEMP".into(), temporary.clone().into_os_string()));
        env.push(("TMP".into(), temporary.into_os_string()));
        let mut args = vec!["-I".into(), "-B".into(), "-u".into(), script.into_os_string(), "--stdio".into(),
            "--profile".into(), child_profile.as_os_str().to_owned(), "--bundle".into(), child_bundle.as_os_str().to_owned()];
        if legacy.is_file() { args.extend(["--legacy-store".into(), child_visible_path(legacy)?.into_os_string()]); }
        let spec = ProcessSpec { program: python, args, cwd: child_profile, env,
            stdin: Vec::new(), stdout_limit: MAX_FRAME, stderr_limit: 1024 * 1024 };
        let (sender, receiver) = mpsc::sync_channel(MAX_PENDING);
        let (ready_sender, ready_receiver) = mpsc::sync_channel(1);
        let alive = Arc::new(AtomicBool::new(true));
        let worker_alive = alive.clone();
        let ready = Arc::new(AtomicBool::new(false));
        let worker_ready = ready.clone();
        let opencode = Arc::new(crate::opencode::OpenCodeService::new(profile.to_path_buf()));
        let worker = thread::Builder::new().name("kontur-local-backend".into()).spawn(move || {
            run_worker(spec, receiver, ready_sender, worker_ready, codex, opencode);
            worker_alive.store(false, Ordering::Release);
        }).map_err(|_| failure("local_backend_unavailable"))?;
        Ok((Self { sender, alive, ready, thread: Mutex::new(Some(worker)) }, ready_receiver))
    }

    pub(crate) fn request(&self, request: ApiRequest) -> AppResult<ApiResponse> {
        request.validate()?;
        let result = self.call("request", json!({"method":request.method,"path":request.path,"body":request.body,"headers":request.headers}))?;
        let response: ApiResponse = serde_json::from_value(result).map_err(|_| failure("local_backend_protocol"))?;
        if !(100..=599).contains(&response.status) || response.headers.len() > 64
            || response.body_base64.len() > MAX_RESPONSE_BASE64
            || response.headers.iter().any(|(key, value)| key.len() > 128 || value.len() > 8192
                || !key.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
                || value.chars().any(|c| c.is_control())) {
            return Err(failure("local_backend_protocol"));
        }
        Ok(response)
    }

    pub(crate) fn call(&self, operation: &str, fields: Value) -> AppResult<Value> {
        if !["request", "flush", "import_preview", "import_bundle", "export_bundle"].contains(&operation)
            || !fields.is_object() || !self.alive.load(Ordering::Acquire) {
            return Err(failure("local_backend_unavailable"));
        }
        if !self.is_ready() { return Err(failure("local_backend_starting")); }
        let (reply, receiver) = mpsc::sync_channel(1);
        self.sender.try_send(Command::Call { operation: operation.into(), fields, reply })
            .map_err(|_| failure("local_backend_busy"))?;
        receiver.recv_timeout(Duration::from_secs(125)).map_err(|_| failure("local_backend_timeout"))?
    }

    pub(crate) fn shutdown(&self) -> AppResult<()> {
        if self.alive.load(Ordering::Acquire) {
            let (reply, receiver) = mpsc::sync_channel(1);
            self.sender.try_send(Command::Stop { reply }).map_err(|_| failure("local_backend_busy"))?;
            receiver.recv_timeout(Duration::from_secs(5)).map_err(|_| failure("local_backend_timeout"))??;
        }
        if let Ok(mut handle) = self.thread.lock() {
            if handle.as_ref().is_some_and(|t| t.is_finished()) { if let Some(t) = handle.take() { let _ = t.join(); } }
        }
        Ok(())
    }
}

impl Drop for BackendService { fn drop(&mut self) { let _ = self.shutdown(); } }

fn send_frame(process: &mut ManagedProcess, value: &Value) -> AppResult<()> {
    let mut bytes = serde_json::to_vec(value).map_err(|_| failure("local_backend_protocol"))?;
    if bytes.len() >= MAX_FRAME { return Err(failure("local_request_invalid")); }
    bytes.push(b'\n');
    process.send(&bytes).map_err(|_| failure("local_backend_unavailable"))
}

fn gateway(codex: &CodexService, opencode: &crate::opencode::OpenCodeService, frame: &Value) -> AppResult<Value> {
    let id = frame.get("callId").and_then(Value::as_str).filter(|s| !s.is_empty() && s.len() <= 128)
        .ok_or_else(|| failure("local_backend_protocol"))?;
    let operation = frame.get("operation").and_then(Value::as_str).unwrap_or_default();
    let payload = frame.get("payload").cloned().unwrap_or_else(|| json!({}));
    let opencode_result = match operation {
        "opencode_status" => Some(Ok(opencode.status())),
        "opencode_start" => Some(opencode.start(serde_json::from_value(payload.clone()).map_err(|_| failure("local_backend_protocol"))?)),
        "opencode_poll" | "opencode_cancel" => {
            let request_id = payload.get("requestId").and_then(Value::as_str)
                .filter(|s| !s.is_empty() && s.len() <= 128).ok_or_else(|| failure("local_backend_protocol"))?;
            Some(if operation == "opencode_poll" { opencode.poll(request_id) } else { opencode.cancel(request_id) })
        }
        _ => None,
    };
    if let Some(result) = opencode_result {
        return Ok(match result {
            Ok(value) => json!({"op":"gateway_result","callId":id,"ok":true,"result":value}),
            Err(error) => json!({"op":"gateway_result","callId":id,"ok":false,"error":error}),
        });
    }
    let result = match operation {
        "codex_status" => Ok(serde_json::to_value(codex.status())),
        "codex_start" => {
            let request: CodexRequest = serde_json::from_value(payload).map_err(|_| failure("local_backend_protocol"))?;
            codex.start(request).map(serde_json::to_value)
        }
        "codex_poll" | "codex_cancel" => {
            let request_id = payload.get("requestId").and_then(Value::as_str)
                .filter(|s| !s.is_empty() && s.len() <= 256).ok_or_else(|| failure("local_backend_protocol"))?;
            (if operation == "codex_poll" { codex.poll(request_id) } else { codex.cancel(request_id) }).map(serde_json::to_value)
        }
        _ => return Err(failure("local_backend_protocol")),
    };
    Ok(match result {
        Ok(Ok(value)) => json!({"op":"gateway_result","callId":id,"ok":true,"result":value}),
        Ok(Err(_)) => return Err(failure("local_backend_protocol")),
        Err(error) => json!({"op":"gateway_result","callId":id,"ok":false,"error":error}),
    })
}

fn feed_frames(buffer: &mut Vec<u8>, bytes: &[u8], limit: usize,
    mut consume: impl FnMut(Value) -> AppResult<()>) -> AppResult<()> {
    // A single pipe poll may contain several individually valid large replies.
    // Bound each incomplete frame, rather than their combined poll size.
    for part in bytes.split_inclusive(|b| *b == b'\n') {
        if buffer.len().saturating_add(part.len()) > limit { return Err(failure("local_backend_protocol")); }
        buffer.extend_from_slice(part);
        if part.last() != Some(&b'\n') { continue; }
        let frame: Value = serde_json::from_slice(buffer).map_err(|_| failure("local_backend_protocol"))?;
        buffer.clear();
        consume(frame)?;
    }
    Ok(())
}

fn run_worker(spec: ProcessSpec, receiver: Receiver<Command>, ready: SyncSender<AppResult<()>>, ready_state: Arc<AtomicBool>, codex: Arc<CodexService>, opencode: Arc<crate::opencode::OpenCodeService>) {
    let mut process = match ManagedProcess::spawn_persistent(spec) {
        Ok(child) => child, Err(_) => { let _ = ready.send(Err(failure("local_backend_unavailable"))); return; }
    };
    let mut pending = BTreeMap::<u64, Pending>::new();
    let mut next_id = 1u64;
    let mut buffer = Vec::<u8>::new();
    let mut initialized = false;
    let startup_deadline = Instant::now() + Duration::from_secs(180);
    let outcome = (|| -> AppResult<()> {
        loop {
            for _ in 0..16 {
                match receiver.try_recv() {
                    Ok(Command::Stop { reply }) => {
                        let result = opencode.shutdown().and_then(|_| process.cancel().map_err(|_| failure("local_backend_unavailable")));
                        let _ = reply.send(result.clone());
                        return result;
                    }
                    Ok(Command::Call { operation, fields, reply }) => {
                        if pending.len() >= MAX_PENDING { let _ = reply.send(Err(failure("local_backend_busy"))); continue; }
                        let mut object = fields.as_object().cloned().ok_or_else(|| failure("local_request_invalid"))?;
                        object.insert("id".into(), json!(next_id)); object.insert("op".into(), json!(operation));
                        send_frame(&mut process, &Value::Object(object))?;
                        pending.insert(next_id, Pending { reply, deadline: Instant::now() + Duration::from_secs(120) });
                        next_id = next_id.checked_add(1).ok_or_else(|| failure("local_backend_protocol"))?;
                    }
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => return Ok(()),
                }
            }
            let snapshot = process.poll().map_err(|_| failure("local_backend_unavailable"))?;
            if snapshot.output_overflow || snapshot.exit_code.is_some() { return Err(failure("local_backend_unavailable")); }
            feed_frames(&mut buffer, &snapshot.stdout, MAX_FRAME, |frame| {
                if frame.get("event").and_then(Value::as_str) == Some("ready") {
                    if initialized || frame.get("protocol").and_then(Value::as_u64) != Some(1) { return Err(failure("local_backend_protocol")); }
                    initialized = true; ready_state.store(true, Ordering::Release);
                    let _ = ready.try_send(Ok(())); return Ok(());
                }
                if frame.get("event").and_then(Value::as_str) == Some("gateway") {
                    send_frame(&mut process, &gateway(&codex, &opencode, &frame)?)?; return Ok(());
                }
                if frame.get("event").is_some() || !initialized { return Err(failure("local_backend_protocol")); }
                let id = frame.get("id").and_then(Value::as_u64).ok_or_else(|| failure("local_backend_protocol"))?;
                let request = pending.remove(&id).ok_or_else(|| failure("local_backend_protocol"))?;
                let result = match frame.get("ok").and_then(Value::as_bool) {
                    Some(true) => {
                        let mut object = frame.as_object().cloned().ok_or_else(|| failure("local_backend_protocol"))?;
                        object.remove("id"); object.remove("ok");
                        // Non-HTTP operations return their payload under result.
                        Ok(object.remove("result").unwrap_or(Value::Object(object)))
                    }
                    Some(false) => {
                        let error = frame.get("error").and_then(Value::as_object)
                            .ok_or_else(|| failure("local_backend_protocol"))?;
                        let code = error.get("code").and_then(Value::as_str)
                            .filter(|s| !s.is_empty() && s.len() <= 128 && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_'))
                            .ok_or_else(|| failure("local_backend_protocol"))?;
                        let message = error.get("message").and_then(Value::as_str)
                            .filter(|s| !s.is_empty() && s.len() <= 2048 && !s.chars().any(char::is_control))
                            .ok_or_else(|| failure("local_backend_protocol"))?;
                        Err(AppError::new(code, message))
                    }
                    None => return Err(failure("local_backend_protocol")),
                };
                let _ = request.reply.send(result);
                Ok(())
            })?;
            if (!initialized && Instant::now() >= startup_deadline)
                || pending.values().any(|p| Instant::now() >= p.deadline) { return Err(failure("local_backend_timeout")); }
            thread::sleep(Duration::from_millis(5));
        }
    })();
    let error = outcome.err().unwrap_or_else(|| failure("local_backend_unavailable"));
    let _ = ready.try_send(Err(error.clone()));
    for (_, request) in pending { let _ = request.reply.send(Err(error.clone())); }
    let _ = process.cancel();
}

pub(crate) fn resource_root() -> AppResult<PathBuf> {
    std::env::current_exe().ok().and_then(|p| p.parent().map(|p| p.join("resources")))
        .ok_or_else(|| failure("local_backend_unavailable"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn loading_service_rejects_requests_without_dispatch_and_remains_closable() {
        let service = BackendService::synthetic(|_, _| panic!("request reached a loading child"));
        service.ready.store(false, Ordering::Release);
        assert!(!service.is_ready());
        assert_eq!(service.request(input("/api/v1/local/client-state")).unwrap_err().code, "local_backend_starting");
        assert!(service.shutdown().is_ok());
    }
    fn input(path: &str) -> ApiRequest { ApiRequest { method: "GET".into(), path: path.into(), body: None, headers: BTreeMap::new() } }
    #[test]
    fn child_paths_keep_windows_file_identity_without_verbatim_url_syntax() {
        let root = std::env::temp_dir().join(format!("kontur-child-path-{}", crate::platform::random_token().unwrap()));
        std::fs::create_dir(&root).unwrap();
        let file = root.join("Проверка файла.txt");
        std::fs::write(&file, b"synthetic path identity").unwrap();
        for path in [&root, &file] {
            let canonical = path.canonicalize().unwrap();
            let child = child_visible_path(&canonical).unwrap();
            assert_eq!(child.canonicalize().unwrap(), canonical);
            assert!(!child.as_os_str().to_string_lossy().starts_with(r"\\?\"));
            assert_eq!(child_visible_path(&child).unwrap(), child);
        }
        assert!(child_visible_path(Path::new("relative")).is_err());
        assert!(child_visible_path(Path::new(r"\\.\NUL")).is_err());
        assert!(child_visible_path(Path::new(r"\\?\GLOBALROOT\Device")).is_err());
        std::fs::remove_file(file).unwrap();
        std::fs::remove_dir(root).unwrap();
    }
    #[test]
    fn admits_original_catalog_and_asset_paths() {
        for path in ["/api/v1/tasks?query=hello%20world", "/api/v1/assets/a/device", "/api/v1/exams/e/items/i"] {
            assert!(input(path).validate().is_ok(), "{path}");
        }
    }
    #[test]
    fn rejects_non_api_urls_and_path_escapes() {
        for path in ["https://example.com/api/v1/tasks", "//api/v1/tasks", "/api/v1/../auth", "/api/v1/%2e%2e/auth",
            "/api/v1/a%2fb", "/api/v1/a\\b", "/api/v1/a#b", "/api/v1/a\nb"] {
            assert!(input(path).validate().is_err(), "{path}");
        }
    }
    #[test]
    fn enforces_method_and_body_bounds() {
        let mut request = input("/api/v1/tasks"); request.method = "CONNECT".into(); assert!(request.validate().is_err());
        request.method = "POST".into(); request.body = Some("x".repeat(MAX_BODY + 1)); assert!(request.validate().is_err());
    }
    #[test]
    fn retains_idempotency_and_sync_tags_without_admitting_credentials() {
        let mut request = input("/api/v1/tasks");
        request.headers.insert("Idempotency-Key".into(), "attempt:unique".into());
        request.headers.insert("X-Sync-Owner".into(), "local-owner".into());
        assert!(request.validate().is_ok());
        request.headers.insert("Authorization".into(), "forbidden".into());
        assert!(request.validate().is_err());
    }
    #[test]
    fn multiple_frames_in_one_poll_are_bounded_individually() {
        let frame = format!("{}\n", json!({"data":"x".repeat(180)}));
        assert!(frame.len() < 256);
        let bytes = frame.repeat(3); assert!(bytes.len() > 256);
        let mut buffer = Vec::new(); let mut count = 0;
        feed_frames(&mut buffer, bytes.as_bytes(), 256, |_| { count += 1; Ok(()) }).unwrap();
        assert_eq!(count, 3); assert!(buffer.is_empty());
        assert!(feed_frames(&mut buffer, &vec![b'x';257], 256, |_| Ok(())).is_err());
    }
    #[test]
    fn frames_survive_every_utf8_pipe_boundary() {
        let bytes = format!("{}\n", json!({"text":"Проверка ✓"})).into_bytes();
        for split in 0..=bytes.len() {
            let mut buffer = Vec::new(); let mut frames = Vec::new();
            feed_frames(&mut buffer, &bytes[..split], 256, |v| { frames.push(v); Ok(()) }).unwrap();
            feed_frames(&mut buffer, &bytes[split..], 256, |v| { frames.push(v); Ok(()) }).unwrap();
            assert_eq!(frames.len(),1); assert_eq!(frames[0]["text"],"Проверка ✓");
        }
    }
}
