//! Windows-only operating-system boundaries. No shell, HTTP client or filesystem IPC.
use std::{ffi::OsString, path::PathBuf};

use crate::domain::{AppError, AppResult};

/// Hard limits for the long-lived private stdio transport, including an active write.
pub const PERSISTENT_FRAME_LIMIT: usize = 32 * 1024 * 1024;
pub const PERSISTENT_OUTPUT_LIMIT: usize = 96 * 1024 * 1024;
pub const PERSISTENT_INPUT_QUEUE_LIMIT: usize = PERSISTENT_FRAME_LIMIT;
pub const PERSISTENT_INPUT_FRAME_COUNT: usize = 64;

#[derive(Debug, Clone, Copy)]
pub struct PlatformError {
    pub code: &'static str,
    pub message: &'static str,
}

impl PlatformError {
    fn process() -> Self {
        Self { code: "process_unavailable", message: "Не удалось безопасно запустить локальный Codex CLI." }
    }
    fn io() -> Self {
        Self { code: "process_io", message: "Не удалось завершить обмен с локальным Codex CLI." }
    }
}

impl std::fmt::Display for PlatformError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.message)
    }
}
impl std::error::Error for PlatformError {}

/// `env` is the entire replacement environment, never an inherited overlay.
pub struct ProcessSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub env: Vec<(OsString, OsString)>,
    pub stdin: Vec<u8>,
    pub stdout_limit: usize,
    pub stderr_limit: usize,
}

pub struct ProcessSnapshot {
    /// Newly drained stdout; each retained byte is delivered at most once.
    pub stdout: Vec<u8>,
    /// Cumulative for `spawn`; bytes since the previous poll for `spawn_persistent`.
    /// Stderr text never leaves the reader.
    pub stderr_bytes: usize,
    /// Available only after process termination and both pipe readers reach EOF.
    pub exit_code: Option<u32>,
    pub output_overflow: bool,
}

fn checksum_error() -> AppError {
    AppError::new("checksum_unavailable", "Не удалось проверить контрольную сумму данных.")
}

#[cfg(windows)]
mod win32 {
    use super::*;
    use std::{
        collections::{BTreeMap, VecDeque},
        ffi::{c_void, OsStr},
        fs::File,
        io::{Read, Write},
        os::windows::{ffi::{OsStrExt, OsStringExt}, io::FromRawHandle},
        path::Path,
        sync::{atomic::{AtomicBool, Ordering}, Arc, Condvar, Mutex},
        thread::{self, JoinHandle},
        time::{Duration, Instant},
    };
    use windows::{
        core::{BOOL, PCWSTR, PWSTR},
        Win32::{
            Foundation::{
                CloseHandle, GetLastError, SetHandleInformation, HANDLE, HANDLE_FLAGS,
                HANDLE_FLAG_INHERIT, HWND, ERROR_ALREADY_EXISTS, WAIT_OBJECT_0,
            },
            Security::{
                Cryptography::{
                    BCryptCloseAlgorithmProvider, BCryptCreateHash, BCryptDestroyHash,
                    BCryptFinishHash, BCryptGenRandom, BCryptHash, BCryptHashData,
                    BCryptOpenAlgorithmProvider, BCRYPT_ALG_HANDLE, BCRYPT_HASH_HANDLE,
                    BCRYPT_OPEN_ALGORITHM_PROVIDER_FLAGS, BCRYPT_SHA256_ALGORITHM,
                    BCRYPT_USE_SYSTEM_PREFERRED_RNG,
                },
                SECURITY_ATTRIBUTES,
            },
            System::{
                JobObjects::{
                    AssignProcessToJobObject, CreateJobObjectW, QueryInformationJobObject,
                    SetInformationJobObject, TerminateJobObject, JobObjectBasicAccountingInformation,
                    JobObjectBasicProcessIdList, JobObjectExtendedLimitInformation,
                    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_BASIC_PROCESS_ID_LIST,
                    JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
                },
                Pipes::CreatePipe,
                Threading::{
                    CreateEventW, CreateMutexW, CreateProcessW, DeleteProcThreadAttributeList,
                    GetExitCodeProcess, InitializeProcThreadAttributeList, OpenProcess, ResumeThread,
                    SetEvent, TerminateProcess, UpdateProcThreadAttribute, WaitForSingleObject,
                    CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
                    EXTENDED_STARTUPINFO_PRESENT, LPPROC_THREAD_ATTRIBUTE_LIST,
                    PROCESS_INFORMATION, PROCESS_SYNCHRONIZE, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES,
                    STARTUPINFOEXW,
                },
            },
            UI::{
                Controls::Dialogs::{
                    CommDlgExtendedError, GetOpenFileNameW, GetSaveFileNameW, OPENFILENAMEW,
                    OFN_DONTADDTORECENT, OFN_EXPLORER, OFN_FILEMUSTEXIST, OFN_NOCHANGEDIR,
                    OFN_OVERWRITEPROMPT, OFN_PATHMUSTEXIST,
                },
                WindowsAndMessaging::{MessageBoxW, IDRETRY, MB_ICONERROR, MB_ICONWARNING, MB_OK, MB_RETRYCANCEL},
            },
        },
    };

    fn wide(value: &OsStr) -> Result<Vec<u16>, PlatformError> {
        let encoded: Vec<_> = value.encode_wide().collect();
        if encoded.contains(&0) { return Err(PlatformError::process()); }
        Ok(encoded.into_iter().chain(Some(0)).collect())
    }

    fn file_api_path(path: &Path) -> AppResult<Vec<u16>> {
        use std::path::{Component, Prefix};
        let local_disk = |value: &Path| matches!(value.components().next(),
            Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)))
            && value.is_absolute();
        let absolute = std::path::absolute(path).map_err(|_| crate::domain::io_error())?;
        if !local_disk(&absolute) { return Err(crate::domain::io_error()); }
        // canonicalize produces a Windows extended-length path. The existing
        // parent can be canonicalized even when the destination does not exist.
        let parent = absolute.parent().ok_or_else(crate::domain::io_error)?;
        let parent = parent.canonicalize().map_err(|_| crate::domain::io_error())?;
        if !local_disk(&parent) { return Err(crate::domain::io_error()); }
        let name = absolute.file_name().ok_or_else(crate::domain::io_error)?;
        wide(parent.join(name).as_os_str()).map_err(|_| crate::domain::io_error())
    }

    struct KernelHandle(usize);
    impl KernelHandle {
        fn new(handle: HANDLE) -> Self { Self(handle.0 as usize) }
        fn raw(&self) -> HANDLE { HANDLE(self.0 as *mut c_void) }
        fn into_file(mut self) -> File {
            let handle = std::mem::replace(&mut self.0, 0);
            // Exactly one File takes ownership of the pipe end.
            unsafe { File::from_raw_handle(handle as *mut c_void) }
        }
    }
    impl Drop for KernelHandle {
        fn drop(&mut self) {
            if self.0 != 0 { unsafe { let _ = CloseHandle(self.raw()); } }
        }
    }

    pub fn sha256(bytes: &[u8]) -> AppResult<String> {
        if bytes.len() > u32::MAX as usize { return Err(checksum_error()); }
        let mut algorithm = BCRYPT_ALG_HANDLE::default();
        unsafe {
            BCryptOpenAlgorithmProvider(&mut algorithm, BCRYPT_SHA256_ALGORITHM,
                PCWSTR::null(), BCRYPT_OPEN_ALGORITHM_PROVIDER_FLAGS(0))
                .ok().map_err(|_| checksum_error())?;
            let mut digest = [0u8; 32];
            let result = BCryptHash(algorithm, None, bytes, &mut digest).ok();
            let _ = BCryptCloseAlgorithmProvider(algorithm, 0);
            result.map_err(|_| checksum_error())?;
            Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
        }
    }

    fn sha256_reader(reader: &mut impl Read, maximum: u64) -> AppResult<String> {
        let mut algorithm = BCRYPT_ALG_HANDLE::default();
        unsafe {
            BCryptOpenAlgorithmProvider(&mut algorithm, BCRYPT_SHA256_ALGORITHM,
                PCWSTR::null(), BCRYPT_OPEN_ALGORITHM_PROVIDER_FLAGS(0))
                .ok().map_err(|_| checksum_error())?;
            let mut hash = BCRYPT_HASH_HANDLE::default();
            let result = (|| -> AppResult<String> {
                BCryptCreateHash(algorithm, &mut hash, None, None, 0)
                    .ok().map_err(|_| checksum_error())?;
                let mut buffer = [0u8; 64 * 1024];
                let mut total = 0u64;
                loop {
                    let count = reader.read(&mut buffer).map_err(|_| checksum_error())?;
                    if count == 0 { break; }
                    total = total.checked_add(count as u64).ok_or_else(checksum_error)?;
                    if total > maximum { return Err(checksum_error()); }
                    BCryptHashData(hash, &buffer[..count], 0).ok().map_err(|_| checksum_error())?;
                }
                let mut digest = [0u8; 32];
                BCryptFinishHash(hash, &mut digest, 0).ok().map_err(|_| checksum_error())?;
                Ok(digest.iter().map(|byte| format!("{byte:02x}")).collect())
            })();
            if hash != BCRYPT_HASH_HANDLE::default() { let _ = BCryptDestroyHash(hash); }
            let _ = BCryptCloseAlgorithmProvider(algorithm, 0);
            result
        }
    }

    pub fn sha256_file(path: &Path, maximum: u64) -> AppResult<String> {
        use std::os::windows::fs::OpenOptionsExt;
        let mut file = std::fs::OpenOptions::new().read(true).share_mode(1)
            .open(path).map_err(|_| checksum_error())?;
        let metadata = file.metadata().map_err(|_| checksum_error())?;
        if !metadata.is_file() || metadata.len() > maximum { return Err(checksum_error()); }
        sha256_reader(&mut file, maximum)
    }

    pub fn random_token() -> AppResult<String> {
        let mut random = [0u8; 32];
        unsafe { BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) }
            .ok().map_err(|_| AppError::new("platform_error", "Не удалось создать локальное подтверждение."))?;
        Ok(random.iter().map(|byte| format!("{byte:02x}")).collect())
    }

    fn dialog(owner: isize, save: bool, attachment: Option<&str>) -> AppResult<Option<PathBuf>> {
        let title = if attachment.is_some() { "Сохранить файл задания" }
            else if save { "Сохранить данные Контура" } else { "Открыть данные Контура" };
        let title: Vec<_> = title.encode_utf16().chain(Some(0)).collect();
        let filter: Vec<_> = if attachment.is_some() { "Файл задания\0*.*\0\0" }
            else { "Данные Контура (*.kontur)\0*.kontur\0\0" }.encode_utf16().collect();
        let extension: Vec<_> = "kontur\0".encode_utf16().collect();
        let mut file = vec![0u16; 32768];
        if save {
            let suggested = attachment.unwrap_or("Контур-данные.kontur");
            for (target, source) in file.iter_mut().zip(suggested.encode_utf16().take(240)) { *target = source; }
        }
        let mut value = OPENFILENAMEW::default();
        value.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
        value.hwndOwner = HWND(owner as *mut c_void);
        value.lpstrFilter = PCWSTR(filter.as_ptr());
        value.lpstrFile = PWSTR(file.as_mut_ptr());
        value.nMaxFile = file.len() as u32;
        value.lpstrTitle = PCWSTR(title.as_ptr());
        if attachment.is_none() { value.lpstrDefExt = PCWSTR(extension.as_ptr()); }
        value.Flags = OFN_EXPLORER | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_DONTADDTORECENT
            | if save { OFN_OVERWRITEPROMPT } else { OFN_FILEMUSTEXIST };
        let accepted = unsafe {
            if save { GetSaveFileNameW(&mut value) } else { GetOpenFileNameW(&mut value) }
        };
        if !accepted.as_bool() {
            return if unsafe { CommDlgExtendedError() }.0 == 0 { Ok(None) }
                else { Err(AppError::new("dialog_error", "Не удалось открыть окно выбора файла.")) };
        }
        let end = file.iter().position(|&unit| unit == 0).ok_or_else(||
            AppError::new("file_error", "Не удалось прочитать выбранный путь."))?;
        let path = PathBuf::from(OsString::from_wide(&file[..end]));
        if !local_absolute_path(&path) || (attachment.is_none() && !path.extension().and_then(|v| v.to_str()).is_some_and(|v| v.eq_ignore_ascii_case("kontur"))) {
            return Err(AppError::new("invalid_path", "Выберите локальный файл с расширением .kontur."));
        }
        Ok(Some(path))
    }

    // Reject network and device namespaces. A dialog is the sole source of paths.
    fn local_absolute_path(path: &Path) -> bool {
        use std::path::{Component, Prefix};
        matches!(path.components().next(), Some(Component::Prefix(p)) if matches!(p.kind(), Prefix::Disk(_)))
            && path.is_absolute()
    }
    pub fn open_bundle(owner: isize) -> AppResult<Option<PathBuf>> { dialog(owner, false, None) }
    pub fn save_bundle(owner: isize) -> AppResult<Option<PathBuf>> { dialog(owner, true, None) }
    pub fn save_attachment(owner: isize, suggested: &str) -> AppResult<Option<PathBuf>> {
        let safe: String = suggested.chars().map(|c| if c.is_control() || "\\/:*?\"<>|".contains(c) { '_' } else { c })
            .take(200).collect();
        let safe = safe.trim_matches(['.', ' ']);
        dialog(owner, true, Some(if safe.is_empty() { "Файл-задания" } else { safe }))
    }

    pub fn close_retry(owner: isize, ai_unavailable: bool) -> bool {
        let text: Vec<_> = if ai_unavailable {
            "Окно осталось открытым. Документы доступны; ИИ недоступен до перезапуска приложения. Повторить закрытие?\0"
        } else { "Окно осталось открытым: сохранение не подтверждено. Повторить сохранение и закрытие?\0" }
            .encode_utf16().collect();
        let title: Vec<_> = "Контур Локальный\0".encode_utf16().collect();
        (unsafe { MessageBoxW(Some(HWND(owner as *mut c_void)), PCWSTR(text.as_ptr()), PCWSTR(title.as_ptr()), MB_RETRYCANCEL | MB_ICONWARNING) }) == IDRETRY
    }

    pub fn startup_error() {
        let text: Vec<_> = "Не удалось открыть Контур Локальный. Проверьте наличие Microsoft Edge WebView2 Runtime и доступ к локальной папке приложения.\0".encode_utf16().collect();
        let title: Vec<_> = "Контур Локальный\0".encode_utf16().collect();
        unsafe { let _ = MessageBoxW(None, PCWSTR(text.as_ptr()), PCWSTR(title.as_ptr()), MB_OK | MB_ICONERROR); }
    }

    pub fn write_selected_file(path: &Path, bytes: &[u8]) -> AppResult<()> {
        use windows::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH};
        let temporary = path.with_file_name(format!(".kontur-{}.tmp", random_token()?));
        let mut created = false;
        let result = (|| {
            let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&temporary)
                .map_err(|_| crate::domain::io_error())?;
            created = true;
            for chunk in bytes.chunks(64 * 1024) { file.write_all(chunk).map_err(|_| crate::domain::io_error())?; }
            file.sync_all().map_err(|_| crate::domain::io_error())?;
            drop(file);
            let from = file_api_path(&temporary)?;
            let to = file_api_path(path)?;
            unsafe { MoveFileExW(PCWSTR(from.as_ptr()), PCWSTR(to.as_ptr()), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) }
                .map_err(|_| crate::domain::io_error())
        })();
        if created && result.is_err() { let _ = std::fs::remove_file(&temporary); }
        result
    }

    /// Atomically publish a completed same-directory transfer without clobbering
    /// an existing destination. This works on FAT/exFAT as well as NTFS.
    pub fn publish_noclobber(temporary: &Path, target: &Path) -> AppResult<()> {
        use windows::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_WRITE_THROUGH};
        if temporary.parent() != target.parent() || temporary == target {
            return Err(crate::domain::io_error());
        }
        let from = file_api_path(temporary)?;
        let to = file_api_path(target)?;
        unsafe { MoveFileExW(PCWSTR(from.as_ptr()), PCWSTR(to.as_ptr()), MOVEFILE_WRITE_THROUGH) }
            .map_err(|_| crate::domain::io_error())
    }

    /// The separate offline identity never contends with the old server client.
    pub struct Instance {
        mutex: KernelHandle,
        event: KernelHandle,
        stopped: Arc<AtomicBool>,
        listener: Option<JoinHandle<()>>,
    }
    impl Instance {
        pub fn claim(identifier: &str) -> Result<Option<Self>, PlatformError> {
            if !valid_app_identity(identifier) { return Err(PlatformError::process()); }
            let name = wide(OsStr::new(&format!("Local\\Kontur.Offline.{identifier}")))?;
            let activation = wide(OsStr::new(&format!("Local\\Kontur.Offline.{identifier}.Activate")))?;
            unsafe {
                let mutex = KernelHandle::new(CreateMutexW(None, false, PCWSTR(name.as_ptr()))
                    .map_err(|_| PlatformError::process())?);
                let exists = GetLastError() == ERROR_ALREADY_EXISTS;
                let event = KernelHandle::new(CreateEventW(None, false, false, PCWSTR(activation.as_ptr()))
                    .map_err(|_| PlatformError::process())?);
                if exists {
                    let _ = SetEvent(event.raw());
                    return Ok(None);
                }
                Ok(Some(Self { mutex, event, stopped: Arc::new(AtomicBool::new(false)), listener: None }))
            }
        }
        pub fn listen(&mut self, app: tauri::AppHandle) -> Result<(), PlatformError> {
            let event = self.event.0;
            let stopped = self.stopped.clone();
            self.listener = Some(thread::Builder::new().name("kontur-activation".into()).spawn(move || {
                use tauri::Manager;
                while !stopped.load(Ordering::Acquire) {
                    if unsafe { WaitForSingleObject(HANDLE(event as *mut c_void), 500) } == WAIT_OBJECT_0 {
                        if stopped.load(Ordering::Acquire) { break; }
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.unminimize();
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                }
            }).map_err(|_| PlatformError::process())?);
            Ok(())
        }
    }

    fn valid_app_identity(identifier: &str) -> bool {
        (identifier == "app.kontur.offline.desktop" || identifier.starts_with("app.kontur.offline.desktop.qa."))
            && identifier.len() <= 128
            && identifier.bytes().all(|value| value.is_ascii_alphanumeric() || value == b'.' || value == b'-')
            && identifier.split('.').all(|part| !part.is_empty())
    }
    impl Drop for Instance {
        fn drop(&mut self) {
            self.stopped.store(true, Ordering::Release);
            unsafe { let _ = SetEvent(self.event.raw()); }
            if let Some(thread) = self.listener.take() { let _ = thread.join(); }
            // The handles stay alive through the listener's final wait.
            let _ = &self.mutex;
        }
    }

    #[derive(Default)]
    struct Output {
        stdout: Vec<u8>,
        stdout_bytes: usize,
        stderr_bytes: usize,
        stdout_done: bool,
        stderr_done: bool,
        stdin_done: bool,
        overflow: bool,
        io_failed: bool,
        persistent_stopped: bool,
    }

    struct AttributeList {
        // usize gives the opaque Windows allocation its required alignment.
        _allocation: Vec<usize>,
        pointer: LPPROC_THREAD_ATTRIBUTE_LIST,
    }
    impl AttributeList {
        fn handles(handles: &[HANDLE]) -> Result<Self, PlatformError> {
            let mut size = 0usize;
            unsafe { let _ = InitializeProcThreadAttributeList(None, 1, None, &mut size); }
            if size == 0 { return Err(PlatformError::process()); }
            let mut allocation = vec![0usize; size.div_ceil(std::mem::size_of::<usize>())];
            let pointer = LPPROC_THREAD_ATTRIBUTE_LIST(allocation.as_mut_ptr().cast());
            unsafe { InitializeProcThreadAttributeList(Some(pointer), 1, None, &mut size) }
                .map_err(|_| PlatformError::process())?;
            let result = Self { _allocation: allocation, pointer };
            unsafe { UpdateProcThreadAttribute(pointer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                Some(handles.as_ptr().cast()), std::mem::size_of_val(handles), None, None) }
                .map_err(|_| PlatformError::process())?;
            Ok(result)
        }
    }
    impl Drop for AttributeList {
        fn drop(&mut self) { unsafe { DeleteProcThreadAttributeList(self.pointer); } }
    }

    fn pipe() -> Result<(KernelHandle, KernelHandle), PlatformError> {
        let mut read = HANDLE::default();
        let mut write = HANDLE::default();
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: std::ptr::null_mut(), bInheritHandle: BOOL(1),
        };
        unsafe { CreatePipe(&mut read, &mut write, Some(&attributes), 0) }
            .map_err(|_| PlatformError::process())?;
        Ok((KernelHandle::new(read), KernelHandle::new(write)))
    }

    /// Quote one argv element using Microsoft's backslash/quote rules.
    fn quote(argument: &OsStr) -> Result<Vec<u16>, PlatformError> {
        let argument: Vec<_> = argument.encode_wide().collect();
        if argument.contains(&0) { return Err(PlatformError::process()); }
        let mut result = vec![b'"' as u16];
        let mut slashes = 0;
        for unit in argument {
            if unit == b'\\' as u16 { slashes += 1; continue; }
            if unit == b'"' as u16 {
                result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2 + 1));
            } else {
                result.extend(std::iter::repeat_n(b'\\' as u16, slashes));
            }
            result.push(unit);
            slashes = 0;
        }
        result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2));
        result.push(b'"' as u16);
        Ok(result)
    }

    fn environment(values: &[(OsString, OsString)]) -> Result<Vec<u16>, PlatformError> {
        let mut values = values.iter().map(|(key, value)| {
            let key: Vec<_> = key.encode_wide().collect();
            let value: Vec<_> = value.encode_wide().collect();
            if key.is_empty() || key.contains(&0) || key.contains(&(b'=' as u16)) || value.contains(&0) {
                return Err(PlatformError::process());
            }
            let order = String::from_utf16(&key).map_err(|_| PlatformError::process())?.to_uppercase();
            Ok((order, key, value))
        }).collect::<Result<Vec<_>, _>>()?;
        values.sort_by(|a, b| a.0.cmp(&b.0));
        if values.windows(2).any(|pair| pair[0].0 == pair[1].0) { return Err(PlatformError::process()); }
        let mut result = Vec::new();
        for (_, key, value) in values {
            result.extend(key);
            result.push(b'=' as u16);
            result.extend(value);
            result.push(0);
        }
        if result.is_empty() { result.push(0); }
        result.push(0);
        if result.len() > 32767 { return Err(PlatformError::process()); }
        Ok(result)
    }

    fn reader(mut file: File, output: Arc<Mutex<Output>>, stdout: bool, limit: usize) -> Result<JoinHandle<()>, PlatformError> {
        thread::Builder::new().name(if stdout { "codex-stdout" } else { "codex-stderr" }.into()).spawn(move || {
            let mut buffer = [0u8; 8192];
            loop {
                let count = match file.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) => count,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) if error.kind() == std::io::ErrorKind::BrokenPipe => break,
                    Err(_) => {
                        if let Ok(mut state) = output.lock() { state.io_failed = true; }
                        break;
                    }
                };
                let Ok(mut state) = output.lock() else { break; };
                if stdout {
                    let remaining = limit.saturating_sub(state.stdout_bytes);
                    state.stdout.extend_from_slice(&buffer[..count.min(remaining)]);
                    state.stdout_bytes = state.stdout_bytes.saturating_add(count);
                    if state.stdout_bytes > limit { state.overflow = true; }
                } else {
                    state.stderr_bytes = state.stderr_bytes.saturating_add(count);
                    if state.stderr_bytes > limit { state.overflow = true; }
                }
            }
            if let Ok(mut state) = output.lock() {
                if stdout { state.stdout_done = true; } else { state.stderr_done = true; }
            }
        }).map_err(|_| PlatformError::process())
    }

    fn persistent_input_error() -> PlatformError {
        PlatformError { code: "process_input", message: "Не удалось передать сообщение локальному процессу." }
    }

    #[derive(Default)]
    struct InputQueue {
        frames: VecDeque<Vec<u8>>,
        // Includes the frame currently owned by the writer, not just queued frames.
        bytes: usize,
        active_bytes: usize,
        closed: bool,
    }

    struct PersistentInput {
        queue: Mutex<InputQueue>,
        available: Condvar,
    }
    impl PersistentInput {
        fn new(initial: Vec<u8>) -> Result<Self, PlatformError> {
            if initial.len() > PERSISTENT_FRAME_LIMIT { return Err(persistent_input_error()); }
            let mut queue = InputQueue::default();
            if !initial.is_empty() {
                queue.frames.try_reserve(1).map_err(|_| persistent_input_error())?;
                queue.bytes = initial.len();
                queue.frames.push_back(initial);
            }
            Ok(Self { queue: Mutex::new(queue), available: Condvar::new() })
        }

        fn send(&self, bytes: &[u8]) -> Result<(), PlatformError> {
            if bytes.len() > PERSISTENT_FRAME_LIMIT { return Err(persistent_input_error()); }
            let mut queue = self.queue.lock().map_err(|_| persistent_input_error())?;
            if queue.closed { return Err(persistent_input_error()); }
            if bytes.is_empty() { return Ok(()); }
            let frame_count = queue.frames.len() + usize::from(queue.active_bytes != 0);
            if frame_count >= PERSISTENT_INPUT_FRAME_COUNT
                || bytes.len() > PERSISTENT_INPUT_QUEUE_LIMIT.saturating_sub(queue.bytes) {
                return Err(persistent_input_error());
            }
            let mut frame = Vec::new();
            frame.try_reserve_exact(bytes.len()).map_err(|_| persistent_input_error())?;
            frame.extend_from_slice(bytes);
            queue.frames.try_reserve(1).map_err(|_| persistent_input_error())?;
            queue.bytes += frame.len();
            queue.frames.push_back(frame);
            drop(queue);
            self.available.notify_one();
            Ok(())
        }

        fn take(&self) -> Result<Option<Vec<u8>>, PlatformError> {
            let mut queue = self.queue.lock().map_err(|_| persistent_input_error())?;
            if queue.active_bytes != 0 { return Err(persistent_input_error()); }
            while !queue.closed && queue.frames.is_empty() {
                queue = self.available.wait(queue).map_err(|_| persistent_input_error())?;
            }
            if queue.closed { return Ok(None); }
            let frame = queue.frames.pop_front().ok_or_else(persistent_input_error)?;
            queue.active_bytes = frame.len();
            Ok(Some(frame))
        }

        fn complete(&self, bytes: usize) -> Result<(), PlatformError> {
            let mut queue = self.queue.lock().map_err(|_| persistent_input_error())?;
            if queue.active_bytes != bytes || bytes > queue.bytes { return Err(persistent_input_error()); }
            queue.bytes -= bytes;
            queue.active_bytes = 0;
            Ok(())
        }

        fn close(&self) {
            if let Ok(mut queue) = self.queue.lock() {
                queue.closed = true;
                queue.frames.clear();
                queue.bytes = queue.active_bytes;
            }
            self.available.notify_all();
        }
    }

    fn persistent_reader(
        mut file: File, output: Arc<Mutex<Output>>, space: Arc<Condvar>, stdout: bool, limit: usize,
    ) -> Result<JoinHandle<()>, PlatformError> {
        thread::Builder::new().name(if stdout { "local-stdout" } else { "local-stderr" }.into()).spawn(move || {
            let mut buffer = [0u8; 8192];
            'read: loop {
                if output.lock().map(|state| state.persistent_stopped).unwrap_or(true) { break; }
                let count = match file.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) => count,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) if error.kind() == std::io::ErrorKind::BrokenPipe => break,
                    Err(_) => {
                        if let Ok(mut state) = output.lock() { state.io_failed = true; }
                        break;
                    }
                };
                if stdout {
                    let mut offset = 0;
                    while offset < count {
                        let Ok(mut state) = output.lock() else { break 'read; };
                        while !state.persistent_stopped && state.stdout.len() >= limit {
                            state = match space.wait(state) { Ok(state) => state, Err(_) => break 'read };
                        }
                        if state.persistent_stopped { break 'read; }
                        match append_persistent_stdout(&mut state.stdout, &buffer[offset..count], limit) {
                            Ok(retained) => offset += retained,
                            Err(_) => { state.io_failed = true; break 'read; }
                        }
                    }
                } else {
                    let Ok(mut state) = output.lock() else { break; };
                    if state.persistent_stopped { break; }
                    state.stderr_bytes = state.stderr_bytes.saturating_add(count).min(limit + 1);
                    if state.stderr_bytes > limit {
                        state.overflow = true;
                        break;
                    }
                }
            }
            drop(file);
            if let Ok(mut state) = output.lock() {
                if stdout { state.stdout_done = true; } else { state.stderr_done = true; }
            }
        }).map_err(|_| PlatformError::process())
    }

    fn append_persistent_stdout(queue: &mut Vec<u8>, bytes: &[u8], limit: usize) -> Result<usize, PlatformError> {
        if limit == 0 || limit > PERSISTENT_OUTPUT_LIMIT || queue.len() > limit {
            return Err(PlatformError::io());
        }
        let retained = (limit - queue.len()).min(bytes.len());
        let required = queue.len() + retained;
        if required > queue.capacity() {
            // Grow geometrically, but never ask Vec's amortized reserve to
            // choose a capacity beyond this queue's explicit byte budget.
            let target = queue.capacity().max(8192).saturating_mul(2).max(required).min(limit);
            queue.try_reserve_exact(target - queue.len()).map_err(|_| PlatformError::io())?;
        }
        queue.extend_from_slice(&bytes[..retained]);
        Ok(retained)
    }

    fn persistent_writer(
        mut file: File, input: Arc<PersistentInput>, output: Arc<Mutex<Output>>,
    ) -> Result<JoinHandle<()>, PlatformError> {
        thread::Builder::new().name("local-stdin".into()).spawn(move || {
            let mut failed = false;
            loop {
                let frame = match input.take() {
                    Ok(Some(frame)) => frame,
                    Ok(None) => break,
                    Err(_) => { failed = true; break; }
                };
                let length = frame.len();
                let written = file.write_all(&frame);
                // Release the allocation before admitting replacement input bytes.
                drop(frame);
                let completed = input.complete(length);
                if written.is_err() || completed.is_err() { failed = true; break; }
            }
            input.close();
            drop(file);
            if let Ok(mut state) = output.lock() {
                state.stdin_done = true;
                if failed { state.io_failed = true; }
            }
        }).map_err(|_| PlatformError::process())
    }

    pub struct ManagedProcess {
        job: Option<KernelHandle>,
        process: KernelHandle,
        output: Arc<Mutex<Output>>,
        threads: Vec<JoinHandle<()>>,
        cancelled: bool,
        persistent_input: Option<Arc<PersistentInput>>,
        stdout_space: Option<Arc<Condvar>>,
        shutdown_until: Option<Instant>,
        tree_shutdown_requested: bool,
        tree_shutdown_confirmed: bool,
        tree_shutdown_failed: bool,
        shutdown_members: BTreeMap<u32, KernelHandle>,
        shutdown_total_processes: Option<u32>,
    }
    impl ManagedProcess {
        pub fn spawn(spec: ProcessSpec) -> Result<Self, PlatformError> {
            Self::spawn_inner(spec, false)
        }

        /// A long-lived private stdio process. Stdout is backpressured at the
        /// configured queue limit; polling drains it without a lifetime cap.
        pub fn spawn_persistent(spec: ProcessSpec) -> Result<Self, PlatformError> {
            Self::spawn_inner(spec, true).map_err(|error| PlatformError {
                code: error.code, message: "Не удалось безопасно запустить локальный процесс.",
            })
        }

        fn spawn_inner(mut spec: ProcessSpec, persistent: bool) -> Result<Self, PlatformError> {
            let stdin_limit = if persistent { PERSISTENT_FRAME_LIMIT } else { 2 * 1024 * 1024 };
            let stdout_limit = if persistent { PERSISTENT_OUTPUT_LIMIT } else { 8 * 1024 * 1024 };
            if !spec.program.is_absolute() || !spec.program.is_file() || !spec.cwd.is_absolute()
                || !spec.cwd.is_dir() || spec.stdin.len() > stdin_limit
                || spec.stdout_limit == 0 || spec.stdout_limit > stdout_limit
                || spec.stderr_limit == 0 || spec.stderr_limit > 1024 * 1024 {
                return Err(PlatformError::process());
            }
            let persistent_input = if persistent {
                Some(Arc::new(PersistentInput::new(std::mem::take(&mut spec.stdin))?))
            } else { None };
            let stdout_space = persistent.then(|| Arc::new(Condvar::new()));
            let application = wide(spec.program.as_os_str())?;
            let directory = wide(spec.cwd.as_os_str())?;
            let mut command = quote(spec.program.as_os_str())?;
            for argument in &spec.args {
                command.push(b' ' as u16);
                command.extend(quote(argument)?);
            }
            command.push(0);
            if command.len() > 32767 { return Err(PlatformError::process()); }
            let environment = environment(&spec.env)?;
            let (stdin_read, stdin_write) = pipe()?;
            let (stdout_read, stdout_write) = pipe()?;
            let (stderr_read, stderr_write) = pipe()?;
            for handle in [&stdin_write, &stdout_read, &stderr_read] {
                unsafe { SetHandleInformation(handle.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)) }
                    .map_err(|_| PlatformError::process())?;
            }
            let inherited = [stdin_read.raw(), stdout_write.raw(), stderr_write.raw()];
            let attributes = AttributeList::handles(&inherited)?;
            let job = KernelHandle::new(unsafe { CreateJobObjectW(None, PCWSTR::null()) }
                .map_err(|_| PlatformError::process())?);
            let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            unsafe { SetInformationJobObject(job.raw(), JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of_val(&limits) as u32) }.map_err(|_| PlatformError::process())?;
            let mut startup = STARTUPINFOEXW::default();
            startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = stdin_read.raw();
            startup.StartupInfo.hStdOutput = stdout_write.raw();
            startup.StartupInfo.hStdError = stderr_write.raw();
            startup.lpAttributeList = attributes.pointer;
            let mut info = PROCESS_INFORMATION::default();
            unsafe { CreateProcessW(PCWSTR(application.as_ptr()), Some(PWSTR(command.as_mut_ptr())),
                None, None, true,
                CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                Some(environment.as_ptr().cast()), PCWSTR(directory.as_ptr()), &startup.StartupInfo, &mut info) }
                .map_err(|_| PlatformError::process())?;
            let process = KernelHandle::new(info.hProcess);
            let primary_thread = KernelHandle::new(info.hThread);
            if unsafe { AssignProcessToJobObject(job.raw(), process.raw()) }.is_err() {
                // The only non-Job termination: this process is still suspended and
                // has never run or created a child. Do not resume an unowned child.
                unsafe { let _ = TerminateProcess(process.raw(), 1); let _ = WaitForSingleObject(process.raw(), 2000); }
                return Err(PlatformError::process());
            }
            drop((stdin_read, stdout_write, stderr_write, attributes));
            let output = Arc::new(Mutex::new(Output::default()));
            let mut child = Self { job: Some(job), process, output: output.clone(), threads: Vec::new(),
                cancelled: false, persistent_input, stdout_space, shutdown_until: None,
                tree_shutdown_requested: false, tree_shutdown_confirmed: false,
                tree_shutdown_failed: false, shutdown_members: BTreeMap::new(), shutdown_total_processes: None };
            if let Some(space) = &child.stdout_space {
                child.threads.push(persistent_reader(stdout_read.into_file(), output.clone(), space.clone(), true, spec.stdout_limit)?);
                child.threads.push(persistent_reader(stderr_read.into_file(), output.clone(), space.clone(), false, spec.stderr_limit)?);
            } else {
                child.threads.push(reader(stdout_read.into_file(), output.clone(), true, spec.stdout_limit)?);
                child.threads.push(reader(stderr_read.into_file(), output.clone(), false, spec.stderr_limit)?);
            }
            if let Some(input) = &child.persistent_input {
                child.threads.push(persistent_writer(stdin_write.into_file(), input.clone(), output.clone())?);
            } else {
                let mut input = stdin_write.into_file();
                child.threads.push(thread::Builder::new().name("codex-stdin".into()).spawn(move || {
                    let result = input.write_all(&spec.stdin);
                    drop(input);
                    if let Ok(mut state) = output.lock() {
                        state.stdin_done = true;
                        if result.is_err() { state.io_failed = true; }
                    }
                }).map_err(|_| PlatformError::process())?);
            }
            if unsafe { ResumeThread(primary_thread.raw()) } == u32::MAX {
                return Err(PlatformError::process());
            }
            Ok(child)
        }

        /// Admit one frame without blocking on pipe I/O. Success means queued,
        /// not acknowledged by the child. Full, oversized, or closed input fails
        /// without retaining any part of the rejected frame.
        pub fn send(&self, bytes: &[u8]) -> Result<(), PlatformError> {
            let input = self.persistent_input.as_ref().ok_or_else(persistent_input_error)?;
            if self.cancelled || unsafe { WaitForSingleObject(self.process.raw(), 0) } == WAIT_OBJECT_0 {
                input.close();
                return Err(persistent_input_error());
            }
            input.send(bytes)
        }

        fn close_persistent_stdin(&self) {
            if let Some(input) = &self.persistent_input { input.close(); }
        }

        fn stop_persistent_io(&self) {
            self.close_persistent_stdin();
            if let Some(space) = &self.stdout_space {
                if let Ok(mut output) = self.output.lock() { output.persistent_stopped = true; }
                space.notify_all();
            }
        }

        fn shutdown_failure(&mut self) -> PlatformError {
            // Closing the owned Job is still the final fail-safe, but never a
            // substitute for observed completion on a later poll/cancel.
            self.tree_shutdown_failed = true;
            self.stop_persistent_io();
            drop(self.job.take());
            PlatformError { code: "process_shutdown", message: if self.persistent_input.is_some() {
                "Завершение локального процесса пока не подтверждено."
            } else { "Завершение локального Codex CLI пока не подтверждено." } }
        }

        fn retain_job_members(&mut self) -> Result<(), PlatformError> {
            const MAX_MEMBERS: usize = 4096;
            let job = match &self.job {
                Some(job) => job.raw(),
                None => return Err(self.shutdown_failure()),
            };
            let initially_active = if self.shutdown_total_processes.is_none() {
                let accounting = self.job_accounting(job)?;
                self.shutdown_total_processes = Some(accounting.TotalProcesses);
                Some(accounting.ActiveProcesses as usize)
            } else { None };
            // usize storage supplies the alignment and bounded flexible-array
            // capacity required by JOBOBJECT_BASIC_PROCESS_ID_LIST on Win32/64.
            let mut storage = vec![0usize; MAX_MEMBERS + 2];
            let list = storage.as_mut_ptr().cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>();
            unsafe { QueryInformationJobObject(Some(job), JobObjectBasicProcessIdList,
                list.cast(), (storage.len() * std::mem::size_of::<usize>()) as u32, None) }
                .map_err(|_| self.shutdown_failure())?;
            let count = unsafe { (*list).NumberOfProcessIdsInList as usize };
            if count > MAX_MEMBERS || unsafe { (*list).NumberOfAssignedProcesses as usize } > count {
                return Err(self.shutdown_failure());
            }
            if initially_active.is_some_and(|active| active != count) {
                // A member disappearing before its first handle was retained
                // cannot be proved complete from accounting or EOF alone.
                return Err(self.shutdown_failure());
            }
            let ids = unsafe { std::slice::from_raw_parts(
                std::ptr::addr_of!((*list).ProcessIdList).cast::<usize>(), count) };
            for id in ids {
                if self.shutdown_until.is_some_and(|until| Instant::now() >= until) {
                    return Err(self.shutdown_failure());
                }
                let pid = u32::try_from(*id).map_err(|_| self.shutdown_failure())?;
                if pid == 0 { return Err(self.shutdown_failure()); }
                if self.shutdown_members.contains_key(&pid) { continue; }
                if self.shutdown_members.len() >= MAX_MEMBERS { return Err(self.shutdown_failure()); }
                // These handles are used only for waiting. Termination always
                // targets the owned Job, never a PID from an untrusted caller.
                let process = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, pid) }
                    .map_err(|_| self.shutdown_failure())?;
                self.shutdown_members.insert(pid, KernelHandle::new(process));
            }
            Ok(())
        }

        fn job_accounting(&mut self, job: HANDLE) -> Result<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, PlatformError> {
            let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
            unsafe { QueryInformationJobObject(Some(job), JobObjectBasicAccountingInformation,
                (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                std::mem::size_of_val(&accounting) as u32, None) }.map_err(|_| self.shutdown_failure())?;
            Ok(accounting)
        }

        fn begin_tree_shutdown(&mut self) -> Result<(), PlatformError> {
            if self.tree_shutdown_failed { return Err(self.shutdown_failure()); }
            if self.tree_shutdown_confirmed { return Ok(()); }
            self.shutdown_until.get_or_insert_with(|| Instant::now() + Duration::from_secs(2));
            if !self.tree_shutdown_requested {
                self.retain_job_members()?;
                let job = self.job.as_ref().ok_or_else(PlatformError::process)?.raw();
                unsafe { TerminateJobObject(job, 1) }.map_err(|_| self.shutdown_failure())?;
                self.tree_shutdown_requested = true;
            }
            Ok(())
        }

        fn confirm_tree_shutdown(&mut self) -> Result<bool, PlatformError> {
            if self.tree_shutdown_failed { return Err(self.shutdown_failure()); }
            if self.tree_shutdown_confirmed { return Ok(true); }
            self.retain_job_members()?;
            let job = self.job.as_ref().ok_or_else(PlatformError::process)?.raw();
            let accounting = self.job_accounting(job)?;
            // A fork racing the first census could already have vanished from
            // the PID list without a retained process handle. Never acknowledge
            // an unobserved birth by replacing the original census baseline.
            if self.shutdown_total_processes != Some(accounting.TotalProcesses) {
                return Err(self.shutdown_failure());
            }
            // Job accounting alone is not a process-object completion barrier.
            // Keep member handles until every observed process is signaled.
            let signaled = self.shutdown_members.values().all(|process|
                unsafe { WaitForSingleObject(process.raw(), 0) } == WAIT_OBJECT_0);
            if accounting.ActiveProcesses == 0 && signaled {
                self.tree_shutdown_confirmed = true;
                drop(self.job.take());
                self.shutdown_members.clear();
                return Ok(true);
            }
            Ok(false)
        }

        pub fn poll(&mut self) -> Result<ProcessSnapshot, PlatformError> {
            let mut code = 0u32;
            let exited = unsafe { WaitForSingleObject(self.process.raw(), 0) } == WAIT_OBJECT_0;
            if exited {
                self.close_persistent_stdin();
                unsafe { GetExitCodeProcess(self.process.raw(), &mut code) }.map_err(|_| PlatformError::process())?;
                // Preserve buffered stdout on natural exit while stopping and
                // confirming the remaining Job members under one deadline.
                self.begin_tree_shutdown()?;
            }
            let tree_done = exited && self.confirm_tree_shutdown()?;
            let (mut snapshot, io_failed) = {
                let mut output = self.output.lock().map_err(|_| PlatformError::io())?;
                let snapshot = ProcessSnapshot {
                    stdout: std::mem::take(&mut output.stdout),
                    stderr_bytes: output.stderr_bytes,
                    exit_code: (tree_done && output.stdout_done && output.stderr_done && output.stdin_done).then_some(code),
                    output_overflow: output.overflow,
                };
                if self.stdout_space.is_some() { output.stderr_bytes = 0; }
                (snapshot, output.io_failed)
            };
            if let Some(space) = &self.stdout_space { space.notify_all(); }
            let expected_cancel = self.cancelled;
            if snapshot.output_overflow || io_failed { self.cancel()?; }
            if io_failed && !snapshot.output_overflow && !expected_cancel {
                return Err(if self.persistent_input.is_some() {
                    PlatformError { code: "process_io", message: "Не удалось завершить обмен с локальным процессом." }
                } else { PlatformError::io() });
            }
            self.reap_threads();
            if !self.threads.is_empty() { snapshot.exit_code = None; }
            if exited && snapshot.exit_code.is_none()
                && self.shutdown_until.is_some_and(|until| Instant::now() >= until) {
                return Err(self.shutdown_failure());
            }
            Ok(snapshot)
        }

        pub fn cancel(&mut self) -> Result<(), PlatformError> {
            self.stop_persistent_io();
            self.cancelled = true;
            self.begin_tree_shutdown()?;
            let until = *self.shutdown_until.get_or_insert_with(|| Instant::now() + Duration::from_secs(2));
            loop {
                self.reap_threads();
                let exited = unsafe { WaitForSingleObject(self.process.raw(), 0) } == WAIT_OBJECT_0;
                let complete = self.output.lock().map(|state|
                    state.stdout_done && state.stderr_done && state.stdin_done).unwrap_or(false);
                let tree_done = self.confirm_tree_shutdown()?;
                if tree_done && exited && complete && self.threads.is_empty() { return Ok(()); }
                if Instant::now() >= until {
                    return Err(self.shutdown_failure());
                }
                thread::sleep(Duration::from_millis(2));
            }
        }

        fn reap_threads(&mut self) {
            let mut index = 0;
            while index < self.threads.len() {
                if self.threads[index].is_finished() {
                    let thread = self.threads.swap_remove(index);
                    let _ = thread.join();
                } else { index += 1; }
            }
        }
    }
    impl Drop for ManagedProcess {
        fn drop(&mut self) {
            // cancel wakes backpressure waits, terminates the owned Job and
            // confirms members/I/O within one deadline; close is the fail-safe.
            // A failed confirmation is never turned into an unbounded join in
            // Drop; only successful cancel/poll publishes confirmed completion.
            // The finite Codex path retains its original best-effort behavior.
            let _ = self.cancel();
        }
    }

    pub fn secure_webview(window: &tauri::WebviewWindow) -> tauri::Result<()> {
        use windows::core::Interface;
        use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Settings3;
        window.with_webview(|platform| unsafe {
            if let Ok(webview) = platform.controller().CoreWebView2() {
                if let Ok(settings) = webview.Settings() {
                    let _ = settings.SetAreDefaultContextMenusEnabled(false);
                    let _ = settings.SetAreDevToolsEnabled(false);
                    if let Ok(settings) = settings.cast::<ICoreWebView2Settings3>() {
                        let _ = settings.SetAreBrowserAcceleratorKeysEnabled(false);
                    }
                }
            }
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn sha256_matches_published_vectors() {
            assert_eq!(sha256(b"").unwrap(), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
            assert_eq!(sha256(b"abc").unwrap(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        }
        #[test]
        fn streaming_sha256_matches_million_a_vector_and_enforces_bounds() {
            let mut input = std::io::repeat(b'a').take(1_000_000);
            assert_eq!(sha256_reader(&mut input, 1_000_000).unwrap(),
                "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
            assert!(sha256_reader(&mut std::io::repeat(b'a').take(1_000_000), 999_999).is_err());
            struct BrokenReader;
            impl Read for BrokenReader {
                fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                    Err(std::io::Error::other("synthetic read failure"))
                }
            }
            assert!(sha256_reader(&mut BrokenReader, 1_000_000).is_err());
        }
        #[test]
        fn argv_quoting_preserves_quotes_trailing_slashes_and_empty_values() {
            for (input, expected) in [("", "\"\""), ("a b", "\"a b\""), ("x\"y", "\"x\\\"y\""), ("C:\\end\\", "\"C:\\end\\\\\"")] {
                assert_eq!(String::from_utf16(&quote(OsStr::new(input)).unwrap()).unwrap(), expected);
            }
            assert!(quote(OsStr::new("a\0b")).is_err());
        }
        #[test]
        fn replacement_environment_rejects_duplicate_names_and_is_double_null_terminated() {
            assert_eq!(environment(&[]).unwrap(), vec![0, 0]);
            let value = environment(&[("B".into(), "two".into()), ("A".into(), "one".into())]).unwrap();
            assert_eq!(String::from_utf16(&value).unwrap(), "A=one\0B=two\0\0");
            assert!(environment(&[("PATH".into(), "a".into()), ("Path".into(), "b".into())]).is_err());
        }
        #[test]
        fn dialog_paths_are_local_drive_paths_only() {
            assert!(local_absolute_path(Path::new("C:\\data\\one.kontur")));
            for value in ["relative.kontur", "C:relative.kontur", "\\\\server\\share\\one.kontur", "\\\\?\\C:\\one.kontur", "\\\\.\\PhysicalDrive0"] {
                assert!(!local_absolute_path(Path::new(value)));
            }
        }

        #[test]
        fn singleton_identity_accepts_only_the_controlled_production_or_qa_namespace() {
            assert!(valid_app_identity("app.kontur.offline.desktop"));
            assert!(valid_app_identity("app.kontur.offline.desktop.qa.acceptance"));
            for value in ["another.app", "app.kontur.offline.desktop.qa.", "app.kontur.offline.desktop.qa.a\\b", "app.kontur.offline.desktop.qa.a\0b"] {
                assert!(!valid_app_identity(value));
            }
        }

        #[test]
        fn native_publication_supports_long_cyrillic_paths_and_preserves_no_clobber() {
            struct Fixture { root: PathBuf, base: PathBuf }
            impl Drop for Fixture {
                fn drop(&mut self) {
                    let target = self.root.canonicalize().unwrap();
                    let base = self.base.canonicalize().unwrap();
                    assert_eq!(target.parent(), Some(base.as_path()));
                    assert!(target.file_name().unwrap().to_string_lossy().starts_with("kontur-publish-test-"));
                    std::fs::remove_dir_all(target).unwrap();
                }
            }
            let base = std::env::var_os("CARGO_TARGET_DIR").map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target"));
            let root = base.join(format!("kontur-publish-test-{}", random_token().unwrap()));
            std::fs::create_dir(&root).unwrap();
            let fixture = Fixture { root, base };
            let short_source = fixture.root.join("короткий.part");
            let short_target = fixture.root.join("экспорт.kontur");
            assert!(short_target.as_os_str().encode_wide().count() < 260);
            std::fs::write(&short_source, b"short synthetic file").unwrap();
            publish_noclobber(&short_source, &short_target).unwrap();
            assert_eq!(std::fs::read(&short_target).unwrap(), b"short synthetic file");
            let parent = fixture.root.join("a".repeat(110)).join("b".repeat(110));
            std::fs::create_dir_all(&parent).unwrap();
            let source = parent.join("источник.part");
            let target = parent.join("данные.kontur");
            assert!(target.as_os_str().encode_wide().count() > 260);
            std::fs::write(&source, b"complete synthetic file").unwrap();
            publish_noclobber(&source, &target).unwrap();
            assert!(!source.exists());
            assert_eq!(std::fs::read(&target).unwrap(), b"complete synthetic file");
            std::fs::write(&source, b"replacement denied").unwrap();
            assert!(publish_noclobber(&source, &target).is_err());
            assert!(source.exists());
            assert_eq!(std::fs::read(&target).unwrap(), b"complete synthetic file");
            write_selected_file(&target, b"explicit selected-file replacement").unwrap();
            assert_eq!(std::fs::read(&target).unwrap(), b"explicit selected-file replacement");
        }

        #[test]
        fn native_file_paths_reject_network_and_device_namespaces_without_lookup() {
            for value in ["\\\\server\\share\\file.kontur", "\\\\?\\UNC\\server\\share\\file.kontur", "\\\\.\\PhysicalDrive0", "\\\\?\\GLOBALROOT\\Device\\file"] {
                assert!(file_api_path(Path::new(value)).is_err());
            }
        }

        // This test executable is its own harmless fixture. It never invokes
        // Codex, a shell, Python, login or any external service.
        #[test]
        fn process_fixture_child() {
            let Ok(mode) = std::env::var("KONTUR_OFFLINE_PROCESS_FIXTURE") else { return; };
            match mode.as_str() {
                "echo" => {
                    let mut input = Vec::new();
                    std::io::stdin().take(2 * 1024 * 1024).read_to_end(&mut input).unwrap();
                    std::io::stderr().write_all(b"synthetic stderr").unwrap();
                    std::io::stdout().write_all(&input).unwrap();
                    std::io::stdout().write_all(b"KONTUR_FINAL_TAIL").unwrap();
                    std::io::stdout().flush().unwrap();
                    std::process::exit(0);
                }
                "flood" => {
                    let writer = thread::spawn(|| {
                        for _ in 0..64 { std::io::stderr().write_all(&[b'e'; 8192]).unwrap(); }
                    });
                    for _ in 0..64 { std::io::stdout().write_all(&[b'x'; 8192]).unwrap(); }
                    writer.join().unwrap();
                    std::process::exit(0);
                }
                "tree" | "tree_exit" | "tree_after_census" => {
                    use std::os::windows::process::CommandExt;
                    if mode == "tree_after_census" {
                        use std::io::BufRead;
                        println!("KONTUR_WAIT_FORK");
                        std::io::stdout().flush().unwrap();
                        let mut line = String::new();
                        std::io::stdin().lock().read_line(&mut line).unwrap();
                        assert_eq!(line, "KONTUR_FORK\n");
                    }
                    let mut command = std::process::Command::new(std::env::current_exe().unwrap());
                    command.args(["--exact", "platform::win32::tests::process_fixture_child", "--nocapture"])
                        .env_clear().env("SystemRoot", std::env::var_os("SystemRoot").unwrap_or_default())
                        .env("KONTUR_OFFLINE_PROCESS_FIXTURE", "grandchild")
                        .creation_flags(CREATE_NO_WINDOW.0);
                    if mode == "tree_exit" {
                        // EOF from the main process must not stand in for the
                        // separately observed descendant's process signal.
                        command.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null())
                            .stderr(std::process::Stdio::null());
                    }
                    let child = command.spawn().unwrap();
                    println!("KONTUR_DESCENDANT={}", child.id());
                    std::io::stdout().flush().unwrap();
                    if mode == "tree_exit" {
                        let mut line = String::new();
                        std::io::stdin().read_line(&mut line).unwrap();
                        assert_eq!(line, "KONTUR_EXIT\n");
                        std::io::stdout().write_all(b"KONTUR_TREE_FINAL").unwrap();
                        std::io::stdout().flush().unwrap();
                        std::process::exit(0);
                    }
                    loop { thread::sleep(Duration::from_secs(1)); }
                }
                "persistent_echo" => {
                    use std::io::BufRead;
                    println!("KONTUR_PERSISTENT_READY");
                    std::io::stdout().flush().unwrap();
                    let stdin = std::io::stdin();
                    let mut stdin = stdin.lock();
                    let mut line = Vec::new();
                    loop {
                        line.clear();
                        if stdin.read_until(b'\n', &mut line).unwrap() == 0 { break; }
                        if line == b"KONTUR_EXIT\n" { break; }
                        std::io::stderr().write_all(b"synthetic persistent stderr\n").unwrap();
                        std::io::stdout().write_all(b"KONTUR_ECHO=").unwrap();
                        std::io::stdout().write_all(&line).unwrap();
                        std::io::stdout().flush().unwrap();
                    }
                    std::io::stdout().write_all(b"KONTUR_PERSISTENT_TAIL").unwrap();
                    std::io::stdout().flush().unwrap();
                    std::process::exit(0);
                }
                "persistent_flood" => {
                    println!("KONTUR_PERSISTENT_READY");
                    std::io::stdout().flush().unwrap();
                    loop { std::io::stdout().write_all(&[b'p'; 8192]).unwrap(); }
                }
                "persistent_idle" => {
                    println!("KONTUR_PERSISTENT_READY");
                    std::io::stdout().flush().unwrap();
                    loop { thread::sleep(Duration::from_secs(1)); }
                }
                "grandchild" => loop { thread::sleep(Duration::from_secs(1)); },
                _ => panic!("Unknown synthetic fixture mode"),
            }
        }

        fn fixture_process(mode: &str, input: Vec<u8>, output_limit: usize) -> ManagedProcess {
            ManagedProcess::spawn(ProcessSpec {
                program: std::env::current_exe().unwrap(),
                args: ["--exact", "platform::win32::tests::process_fixture_child", "--nocapture"]
                    .into_iter().map(OsString::from).collect(),
                cwd: std::env::temp_dir(),
                env: vec![("SystemRoot".into(), std::env::var_os("SystemRoot").unwrap_or_default()),
                    ("KONTUR_OFFLINE_PROCESS_FIXTURE".into(), mode.into())],
                stdin: input, stdout_limit: output_limit, stderr_limit: output_limit,
            }).unwrap()
        }

        fn persistent_fixture(mode: &str, output_limit: usize) -> ManagedProcess {
            ManagedProcess::spawn_persistent(ProcessSpec {
                program: std::env::current_exe().unwrap(),
                args: ["--exact", "platform::win32::tests::process_fixture_child", "--nocapture"]
                    .into_iter().map(OsString::from).collect(),
                cwd: std::env::temp_dir(),
                env: vec![("SystemRoot".into(), std::env::var_os("SystemRoot").unwrap_or_default()),
                    ("KONTUR_OFFLINE_PROCESS_FIXTURE".into(), mode.into())],
                stdin: Vec::new(), stdout_limit: output_limit, stderr_limit: 1024 * 1024,
            }).unwrap()
        }

        #[test]
        fn persistent_input_bounds_include_active_bytes_and_oversized_frames() {
            let input = PersistentInput::new(vec![b'i'; PERSISTENT_INPUT_QUEUE_LIMIT]).unwrap();
            assert!(input.send(b"x").is_err());
            let active = input.take().unwrap().unwrap();
            assert_eq!(active.len(), PERSISTENT_FRAME_LIMIT);
            assert!(input.send(b"x").is_err(), "An active write still consumes queue capacity");
            drop(active);
            input.complete(PERSISTENT_FRAME_LIMIT).unwrap();
            input.send(b"x").unwrap();
            input.close();
            assert!(input.send(b"x").is_err());
            assert!(input.take().unwrap().is_none());
            assert_eq!(input.queue.lock().unwrap().bytes, 0);
            assert!(PersistentInput::new(vec![b'i'; PERSISTENT_FRAME_LIMIT + 1]).is_err());
        }

        #[test]
        fn persistent_output_bound_does_not_expand_the_input_queue() {
            assert_eq!(PERSISTENT_FRAME_LIMIT, 32 * 1024 * 1024);
            assert_eq!(PERSISTENT_INPUT_QUEUE_LIMIT, 32 * 1024 * 1024);
            let mut output = Vec::new();
            assert_eq!(append_persistent_stdout(&mut output, b"frame", PERSISTENT_OUTPUT_LIMIT).unwrap(), 5);
            assert_eq!(output, b"frame");
            assert!(append_persistent_stdout(&mut output, b"x", PERSISTENT_OUTPUT_LIMIT + 1).is_err());
        }

        #[test]
        fn persistent_input_frame_count_and_close_are_bounded() {
            let input = PersistentInput::new(Vec::new()).unwrap();
            for _ in 0..PERSISTENT_INPUT_FRAME_COUNT { input.send(b"x").unwrap(); }
            assert!(input.send(b"x").is_err());
            let active = input.take().unwrap().unwrap();
            assert!(input.send(b"x").is_err(), "The active frame counts until its allocation is dropped");
            drop(active);
            input.complete(1).unwrap();
            input.send(b"y").unwrap();
            input.close();
            let queue = input.queue.lock().unwrap();
            assert!(queue.closed && queue.frames.is_empty());
            assert_eq!(queue.bytes, 0);
        }

        #[test]
        fn persistent_input_close_wakes_an_idle_writer() {
            let input = Arc::new(PersistentInput::new(Vec::new()).unwrap());
            let waiting = input.clone();
            let (ready_tx, ready_rx) = std::sync::mpsc::channel();
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            let writer = thread::spawn(move || {
                ready_tx.send(()).unwrap();
                done_tx.send(waiting.take().unwrap().is_none()).unwrap();
            });
            ready_rx.recv_timeout(Duration::from_secs(1)).unwrap();
            input.close();
            assert!(done_rx.recv_timeout(Duration::from_secs(1)).unwrap());
            writer.join().unwrap();
        }

        #[test]
        fn persistent_stdout_allocation_and_length_respect_the_queue_budget() {
            let limit = 1003;
            let mut queue = Vec::with_capacity(19);
            for _ in 0..40 {
                let retained = append_persistent_stdout(&mut queue, &[b'x'; 37], limit).unwrap();
                assert!(retained <= 37);
                assert!(queue.len() <= limit);
                assert!(queue.capacity() <= limit);
            }
            assert_eq!(queue.len(), limit);
            assert_eq!(append_persistent_stdout(&mut queue, b"extra", limit).unwrap(), 0);
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_persistent_roundtrips_outlive_the_stdout_queue_and_keep_final_tail() {
            let limit = 1024;
            let mut child = persistent_fixture("persistent_echo", limit);
            let until = Instant::now() + Duration::from_secs(8);
            let mut output = Vec::new();
            let mut stderr_bytes = 0;
            while !output.windows(b"KONTUR_PERSISTENT_READY".len()).any(|value| value == b"KONTUR_PERSISTENT_READY") {
                let snapshot = child.poll().unwrap();
                assert!(snapshot.stdout.len() <= limit && !snapshot.output_overflow);
                output.extend(snapshot.stdout);
                stderr_bytes += snapshot.stderr_bytes;
                assert!(Instant::now() < until, "Persistent fixture not ready");
                thread::sleep(Duration::from_millis(2));
            }
            for index in 0..6 {
                let mut frame = vec![b'a' + index; 4096];
                frame.push(b'\n');
                let mut expected = b"KONTUR_ECHO=".to_vec();
                expected.extend_from_slice(&frame);
                child.send(&frame).unwrap();
                while !output.windows(expected.len()).any(|value| value == expected.as_slice()) {
                    let snapshot = child.poll().unwrap();
                    assert!(snapshot.stdout.len() <= limit && !snapshot.output_overflow);
                    output.extend(snapshot.stdout);
                    stderr_bytes += snapshot.stderr_bytes;
                    assert!(Instant::now() < until, "Persistent frame did not roundtrip");
                    thread::sleep(Duration::from_millis(2));
                }
                assert_eq!(output.windows(expected.len()).filter(|value| *value == expected.as_slice()).count(), 1);
            }
            child.send(b"KONTUR_EXIT\n").unwrap();
            loop {
                let snapshot = child.poll().unwrap();
                assert!(snapshot.stdout.len() <= limit && !snapshot.output_overflow);
                output.extend(snapshot.stdout);
                stderr_bytes += snapshot.stderr_bytes;
                if let Some(code) = snapshot.exit_code { assert_eq!(code, 0); break; }
                assert!(Instant::now() < until, "Persistent exit did not drain");
                thread::sleep(Duration::from_millis(2));
            }
            assert!(output.len() > limit * 20);
            assert!(output.ends_with(b"KONTUR_PERSISTENT_TAIL"));
            assert_eq!(stderr_bytes, 6 * b"synthetic persistent stderr\n".len());
            assert!(child.poll().unwrap().stdout.is_empty());
            assert!(child.send(b"after exit\n").is_err());
            child.cancel().unwrap();
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_persistent_cancel_closes_a_blocked_writer_and_pending_frames() {
            let mut child = persistent_fixture("persistent_idle", PERSISTENT_OUTPUT_LIMIT);
            child.send(&vec![b'i'; 8 * 1024 * 1024]).unwrap();
            let input = child.persistent_input.as_ref().unwrap().clone();
            let until = Instant::now() + Duration::from_secs(8);
            while input.queue.lock().unwrap().active_bytes == 0 {
                assert!(Instant::now() < until, "Writer never took the synthetic frame");
                thread::sleep(Duration::from_millis(2));
            }
            child.send(&vec![b'q'; 1024 * 1024]).unwrap();
            child.cancel().unwrap();
            assert!(child.threads.is_empty());
            let queue = input.queue.lock().unwrap();
            assert!(queue.closed && queue.frames.is_empty());
            assert_eq!(queue.bytes, 0);
            drop(queue);
            assert!(child.send(b"after cancel").is_err());
            assert!(child.poll().unwrap().exit_code.is_some());
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_persistent_drop_joins_backpressured_readers_and_idle_writer() {
            use windows::Win32::System::Threading::{GetProcessId, OpenProcess, PROCESS_SYNCHRONIZE};
            let child = persistent_fixture("persistent_flood", 1024);
            let process = KernelHandle::new(unsafe {
                OpenProcess(PROCESS_SYNCHRONIZE, false, GetProcessId(child.process.raw()))
            }.unwrap());
            let output = child.output.clone();
            let input = child.persistent_input.as_ref().unwrap().clone();
            let until = Instant::now() + Duration::from_secs(8);
            while output.lock().unwrap().stdout.len() < 1024 {
                assert!(Instant::now() < until, "Reader never reached queue backpressure");
                thread::sleep(Duration::from_millis(2));
            }
            assert!(!output.lock().unwrap().overflow);
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            let owner = thread::spawn(move || { drop(child); done_tx.send(()).unwrap(); });
            done_rx.recv_timeout(Duration::from_millis(2500)).unwrap();
            owner.join().unwrap();
            assert_eq!(unsafe { WaitForSingleObject(process.raw(), 0) }, WAIT_OBJECT_0);
            let output = output.lock().unwrap();
            assert!(output.stdin_done && output.stdout_done && output.stderr_done);
            assert!(input.queue.lock().unwrap().closed);
            drop(output);

            // A confirmation failure must not buy another shutdown deadline in
            // Drop. Poison only synthetic bookkeeping; the owned Job still
            // closes and all real I/O threads can terminate and be joined.
            let mut failed = persistent_fixture("persistent_idle", 1024);
            let poisoned = failed.output.clone();
            assert!(thread::spawn(move || {
                let _guard = poisoned.lock().unwrap();
                panic!("synthetic output-state poison");
            }).join().is_err());
            let started = Instant::now();
            assert_eq!(failed.cancel().unwrap_err().code, "process_shutdown");
            assert!(started.elapsed() < Duration::from_millis(2500));
            assert!(failed.threads.is_empty());
            assert_eq!(unsafe { WaitForSingleObject(failed.process.raw(), 0) }, WAIT_OBJECT_0);
            let started = Instant::now();
            drop(failed);
            assert!(started.elapsed() < Duration::from_millis(250));
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_persistent_parent_exit_reaps_the_owned_descendant() {
            use windows::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};
            let mut child = persistent_fixture("tree_exit", 4096);
            let until = Instant::now() + Duration::from_secs(8);
            let mut output = Vec::new();
            let descendant = loop {
                let snapshot = child.poll().unwrap();
                output.extend(snapshot.stdout);
                if let Some(pid) = String::from_utf8_lossy(&output).lines()
                    .find_map(|line| line.strip_prefix("KONTUR_DESCENDANT=").and_then(|value| value.parse::<u32>().ok())) {
                    break KernelHandle::new(unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, pid) }.unwrap());
                }
                assert!(Instant::now() < until, "Persistent descendant not ready");
                thread::sleep(Duration::from_millis(2));
            };
            child.send(b"KONTUR_EXIT\n").unwrap();
            loop {
                let snapshot = child.poll().unwrap();
                output.extend(snapshot.stdout);
                if let Some(code) = snapshot.exit_code { assert_eq!(code, 0); break; }
                assert!(Instant::now() < until, "Exited parent kept its descendant or I/O alive");
                thread::sleep(Duration::from_millis(2));
            }
            assert_eq!(unsafe { WaitForSingleObject(descendant.raw(), 0) }, WAIT_OBJECT_0);
            assert!(child.threads.is_empty());
            assert!(output.ends_with(b"KONTUR_TREE_FINAL"));
            assert!(child.poll().unwrap().exit_code.is_some());
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_persistent_fork_after_census_never_becomes_confirmed() {
            let mut child = persistent_fixture("tree_after_census", 4096);
            let until = Instant::now() + Duration::from_secs(8);
            let mut output = Vec::new();
            loop {
                output.extend(child.poll().unwrap().stdout);
                if output.windows(b"KONTUR_WAIT_FORK".len()).any(|part| part == b"KONTUR_WAIT_FORK") { break; }
                assert!(Instant::now() < until, "Synthetic parent did not reach census barrier");
                thread::sleep(Duration::from_millis(2));
            }
            // Deterministically put a real fork between the first census and
            // termination. Accounting-zero may never erase this uncertainty.
            child.retain_job_members().unwrap();
            child.send(b"KONTUR_FORK\n").unwrap();
            let descendant = loop {
                output.extend(child.poll().unwrap().stdout);
                if let Some(pid) = String::from_utf8_lossy(&output).lines()
                    .find_map(|line| line.strip_prefix("KONTUR_DESCENDANT=").and_then(|value| value.parse::<u32>().ok())) {
                    break KernelHandle::new(unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, pid) }.unwrap());
                }
                assert!(Instant::now() < until, "Synthetic fork did not reach ready barrier");
                thread::sleep(Duration::from_millis(2));
            };
            assert_eq!(child.cancel().unwrap_err().code, "process_shutdown");
            assert_eq!(unsafe { WaitForSingleObject(child.process.raw(), 2000) }, WAIT_OBJECT_0);
            assert_eq!(unsafe { WaitForSingleObject(descendant.raw(), 2000) }, WAIT_OBJECT_0);
            let failed_at = Instant::now();
            assert_eq!(child.cancel().unwrap_err().code, "process_shutdown");
            assert!(failed_at.elapsed() < Duration::from_millis(250));
            assert_eq!(child.poll().err().unwrap().code, "process_shutdown");
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_process_drains_stdin_and_the_final_stdout_tail_once() {
            let input = "Синтетическая строка\n".repeat(4096).into_bytes();
            let mut child = fixture_process("echo", input.clone(), 1024 * 1024);
            let until = Instant::now() + Duration::from_secs(8);
            let mut output = Vec::new();
            loop {
                let snapshot = child.poll().unwrap();
                output.extend(snapshot.stdout);
                assert!(!snapshot.output_overflow);
                if let Some(code) = snapshot.exit_code { assert_eq!(code, 0); break; }
                assert!(Instant::now() < until, "Fixture did not drain before deadline");
                thread::sleep(Duration::from_millis(5));
            }
            assert!(output.ends_with(b"KONTUR_FINAL_TAIL"));
            assert_eq!(output.windows(input.len()).filter(|part| *part == input.as_slice()).count(), 1);
            assert!(child.poll().unwrap().stdout.is_empty());
            child.cancel().unwrap();
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_process_overflow_is_bounded_and_reaped() {
            let limit = 32 * 1024;
            let mut child = fixture_process("flood", Vec::new(), limit);
            let until = Instant::now() + Duration::from_secs(8);
            let mut retained = 0;
            let mut overflow = false;
            loop {
                let snapshot = child.poll().unwrap();
                retained += snapshot.stdout.len();
                overflow |= snapshot.output_overflow;
                assert!(retained <= limit);
                if snapshot.exit_code.is_some() { break; }
                assert!(Instant::now() < until, "Overflow fixture was not reaped");
                thread::sleep(Duration::from_millis(5));
            }
            assert!(overflow);
            child.cancel().unwrap();
        }

        #[test]
        #[ignore = "Requires ROOT isolated harmless process-test lease"]
        fn windows_job_cancel_reaps_child_and_descendant_before_success() {
            use windows::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};
            let mut child = fixture_process("tree", Vec::new(), 64 * 1024);
            let until = Instant::now() + Duration::from_secs(8);
            let mut output = Vec::new();
            let descendant = loop {
                let snapshot = child.poll().unwrap();
                output.extend(snapshot.stdout);
                let text = String::from_utf8_lossy(&output);
                if let Some(pid) = text.lines().find_map(|line| line.strip_prefix("KONTUR_DESCENDANT=")
                    .and_then(|value| value.parse::<u32>().ok())) {
                    break KernelHandle::new(unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, pid) }.unwrap());
                }
                assert!(Instant::now() < until, "Descendant fixture never became ready");
                thread::sleep(Duration::from_millis(5));
            };
            let started = Instant::now();
            child.cancel().unwrap();
            assert!(started.elapsed() < Duration::from_millis(2500));
            assert_eq!(unsafe { WaitForSingleObject(descendant.raw(), 0) }, WAIT_OBJECT_0);
            assert!(child.poll().unwrap().exit_code.is_some());
        }
    }
}

#[cfg(windows)]
pub use win32::{close_retry, open_bundle, publish_noclobber, random_token, save_attachment, save_bundle, secure_webview, sha256, sha256_file, startup_error, write_selected_file, Instance, ManagedProcess};

#[cfg(not(windows))]
pub fn sha256(_: &[u8]) -> AppResult<String> { Err(checksum_error()) }
#[cfg(not(windows))]
pub fn sha256_file(_: &std::path::Path, _: u64) -> AppResult<String> { Err(checksum_error()) }
#[cfg(not(windows))]
pub fn publish_noclobber(_: &std::path::Path, _: &std::path::Path) -> AppResult<()> { Err(crate::domain::io_error()) }
#[cfg(not(windows))]
pub struct ManagedProcess;
#[cfg(not(windows))]
impl ManagedProcess {
    pub fn spawn(_: ProcessSpec) -> Result<Self, PlatformError> { Err(PlatformError::process()) }
    pub fn spawn_persistent(_: ProcessSpec) -> Result<Self, PlatformError> { Err(PlatformError::process()) }
    pub fn send(&self, _: &[u8]) -> Result<(), PlatformError> { Err(PlatformError::io()) }
    pub fn poll(&mut self) -> Result<ProcessSnapshot, PlatformError> { Err(PlatformError::process()) }
    pub fn cancel(&mut self) -> Result<(), PlatformError> { Ok(()) }
}
