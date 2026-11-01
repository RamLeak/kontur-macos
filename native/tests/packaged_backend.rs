//! ROOT-GO-only packaged backend boundary, with synthetic user state.
//! No GUI, Tauri invoke, capabilities, AI, auth/profile access or network probes.
//! Cargo's default run ignores this test. Missing reviewed resources is failure.
#![cfg(windows)]
#![allow(dead_code)] // The included production module also has its own unit tests.

pub use kontur_offline_lib::{domain, platform};
#[path = "../src/codex.rs"]
mod codex;
#[path = "../src/opencode.rs"]
mod opencode;
#[path = "../src/backend.rs"]
mod backend;

use std::{
    collections::BTreeMap,
    fs::{self, OpenOptions},
    io::{self, Write},
    os::windows::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use serde_json::{json, Value};
use rusqlite::{Connection, OpenFlags};

const STATE_PATH: &str = "/api/v1/local/client-state";
const STATE_KEY: &str = "kontur:assistant-technical-mode:v1";
const PREFIX: &str = "kontur-packaged-backend-test-";
const MARKER: &str = ".owned-packaged-backend-test";
const GO: &str = "ROOT_GO_PACKAGED_BACKEND_V1";
const REPARSE_POINT: u32 = 0x400;
const MAX_STATE_RESPONSE_BYTES: usize = 65_536;
const MAX_ASSET_RESPONSE_BYTES: usize = 64_000_000;

// Never unwrap/debug-print application errors, HTTP bodies or imported records.
fn must<T, E>(result: Result<T, E>, label: &'static str) -> T {
    match result { Ok(value) => value, Err(_) => panic!("{label}") }
}
fn need(condition: bool, label: &'static str) { assert!(condition, "{label}"); }

fn ordinary(path: &Path) -> io::Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || metadata.file_attributes() & REPARSE_POINT != 0 {
        return Err(io::Error::other("TEST_ONLY reparse point refused"));
    }
    Ok(metadata)
}

struct OwnedFixture { root: PathBuf, base: PathBuf, identity: Vec<u8>, cleaned: bool }
impl OwnedFixture {
    fn new() -> Self {
        let base = must(std::env::temp_dir().canonicalize(), "TEST_ONLY temp root unavailable");
        need(base.is_absolute() && base.is_dir(), "TEST_ONLY temp root invalid");
        let stamp = must(SystemTime::now().duration_since(UNIX_EPOCH), "TEST_ONLY clock unavailable").as_nanos();
        for attempt in 0..16 {
            let name = format!("{PREFIX}{}-{stamp}-{attempt}", std::process::id());
            let root = base.join(&name);
            match fs::create_dir(&root) {
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                result => must(result, "TEST_ONLY fixture creation failed"),
            }
            let root = must(root.canonicalize(), "TEST_ONLY fixture path unavailable");
            need(root.parent() == Some(base.as_path()), "TEST_ONLY fixture escaped temp root");
            must(ordinary(&root), "TEST_ONLY fixture must not be a reparse point");
            let identity = name.into_bytes();
            let mut marker = must(OpenOptions::new().write(true).create_new(true).open(root.join(MARKER)),
                                  "TEST_ONLY ownership marker creation failed");
            must(marker.write_all(&identity), "TEST_ONLY ownership marker write failed");
            must(marker.sync_all(), "TEST_ONLY ownership marker sync failed");
            return Self { root, base, identity, cleaned: false };
        }
        panic!("TEST_ONLY unique fixture unavailable");
    }

    fn cleanup(&mut self) -> io::Result<()> {
        if self.cleaned { return Ok(()); }
        let root = self.root.canonicalize()?;
        if !root.is_absolute() || root != self.root || root.parent() != Some(self.base.as_path())
            || !root.file_name().is_some_and(|n| n.to_string_lossy().starts_with(PREFIX)) {
            return Err(io::Error::other("TEST_ONLY cleanup target refused"));
        }
        if !ordinary(&root)?.is_dir() || !ordinary(&root.join(MARKER))?.is_file()
            || fs::read(root.join(MARKER))? != self.identity {
            return Err(io::Error::other("TEST_ONLY cleanup ownership refused"));
        }
        // Preflight the entire owned tree without following links/junctions.
        // Deletion repeats these checks; no remove_dir_all follows an unchecked path.
        scan_owned(&root, &root)?;
        erase_owned(&root, &root)?;
        self.cleaned = true;
        Ok(())
    }
}
impl Drop for OwnedFixture {
    fn drop(&mut self) {
        // Preserve rather than traverse if any ownership/reparse check fails.
        let _ = self.cleanup();
    }
}
fn checked_child(path: &Path, root: &Path) -> io::Result<fs::Metadata> {
    let metadata = ordinary(path)?;
    let resolved = path.canonicalize()?;
    if !resolved.starts_with(root) { return Err(io::Error::other("TEST_ONLY child escaped fixture")); }
    Ok(metadata)
}
fn scan_owned(path: &Path, root: &Path) -> io::Result<()> {
    let metadata = checked_child(path, root)?;
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? { scan_owned(&entry?.path(), root)?; }
    } else if !metadata.is_file() { return Err(io::Error::other("TEST_ONLY special file refused")); }
    Ok(())
}
fn erase_owned(path: &Path, root: &Path) -> io::Result<()> {
    let metadata = checked_child(path, root)?;
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? { erase_owned(&entry?.path(), root)?; }
        fs::remove_dir(path)
    } else if metadata.is_file() { fs::remove_file(path) }
    else { Err(io::Error::other("TEST_ONLY special file refused")) }
}

fn resources() -> PathBuf {
    need(std::env::var("KONTUR_PACKAGED_TEST_GO").ok().as_deref() == Some(GO),
         "Explicit ROOT GO is required; test refused before process launch");
    let supplied = std::env::var_os("KONTUR_PACKAGED_TEST_RESOURCES")
        .map(PathBuf::from).unwrap_or_else(|| panic!("Reviewed absolute packaged resources are required"));
    need(supplied.is_absolute(), "Packaged resources must be an absolute reviewed path");
    let root = must(supplied.canonicalize(), "Reviewed packaged resources unavailable");
    need(must(ordinary(&root), "Packaged resources reparse point refused").is_dir(), "Packaged resources must be a directory");
    for relative in ["python/python.exe", "backend/offline_bridge.py", "catalog/catalog.sqlite3"] {
        need(must(ordinary(&root.join(relative)), "Required packaged program missing or indirect").is_file(),
             "Required packaged program is not an ordinary file");
    }
    root
}

struct Running { backend: Option<backend::BackendService> }
impl Running {
    fn start(profile: &Path, bundle: &Path, absent_legacy: &Path, label: &'static str,
             timings: &mut BTreeMap<&'static str, u128>) -> Self {
        need(!absent_legacy.exists(), "TEST_ONLY legacy path must remain absent");
        let began = Instant::now();
        // Constructor is idle: no status/login/start/probe is called by this test.
        let idle_codex = Arc::new(codex::CodexService::new(profile.to_path_buf()));
        let service = must(backend::BackendService::launch(profile, bundle, absent_legacy, idle_codex),
                           "Packaged backend process launch failed");
        let launch_ms = began.elapsed().as_millis();
        need(launch_ms < 5000, "Native window path blocked on backend preparation");
        let max_launch = timings.entry("nativeLaunchMaxMs").or_default();
        *max_launch = (*max_launch).max(launch_ms);
        loop {
            match service.request(backend::ApiRequest { method: "GET".into(), path: STATE_PATH.into(), body: None, headers: BTreeMap::new() }) {
                Ok(response) => { need(response.status == 200, "Packaged startup request failed"); break; }
                Err(error) if error.code == "local_backend_starting" && began.elapsed() < Duration::from_secs(185) => std::thread::sleep(Duration::from_millis(100)),
                Err(_) => panic!("Packaged backend startup/seed readiness failed"),
            }
        }
        timings.insert(label, began.elapsed().as_millis());
        Self { backend: Some(service) }
    }
    fn service(&self) -> &backend::BackendService {
        self.backend.as_ref().expect("TEST_ONLY backend already stopped")
    }
    fn stop(mut self) {
        let service = self.backend.take().expect("TEST_ONLY backend already stopped");
        must(service.shutdown(), "Owned packaged backend shutdown failed");
        drop(service);
    }
}
impl Drop for Running {
    fn drop(&mut self) { if let Some(service) = self.backend.take() { let _ = service.shutdown(); } }
}

// Strict decoder with separate bounds for small state JSON and one attachment.
fn decode_base64(text: &str, maximum: usize) -> Option<Vec<u8>> {
    fn six(byte: u8) -> Option<u8> {
        match byte { b'A'..=b'Z' => Some(byte - b'A'), b'a'..=b'z' => Some(byte - b'a' + 26),
            b'0'..=b'9' => Some(byte - b'0' + 52), b'+' => Some(62), b'/' => Some(63), _ => None }
    }
    let maximum_encoded = (maximum.checked_add(2)? / 3).checked_mul(4)?;
    if text.len() > maximum_encoded || text.len() % 4 != 0 { return None; }
    let padding = if text.ends_with("==") { 2 } else if text.ends_with('=') { 1 } else { 0 };
    let decoded_size = (text.len() / 4).checked_mul(3)?.checked_sub(padding)?;
    if decoded_size > maximum { return None; }
    let mut bytes = Vec::with_capacity(decoded_size);
    let count = text.len() / 4;
    for (index, part) in text.as_bytes().chunks_exact(4).enumerate() {
        let (a, b) = (six(part[0])?, six(part[1])?);
        bytes.push((a << 2) | (b >> 4));
        if part[2] == b'=' {
            if index + 1 != count || part[3] != b'=' || b & 15 != 0 { return None; }
        } else {
            let c = six(part[2])?;
            bytes.push((b << 4) | (c >> 2));
            if part[3] == b'=' {
                if index + 1 != count || c & 3 != 0 { return None; }
            } else { bytes.push((c << 6) | six(part[3])?); }
        }
    }
    if bytes.len() != decoded_size { None } else { Some(bytes) }
}
fn decode_json_base64(text: &str) -> Option<Vec<u8>> {
    decode_base64(text, MAX_STATE_RESPONSE_BYTES)
}

struct SeedAsset { id: String, sha256: String, size: usize }

fn largest_seed_asset(bundle: &Path) -> SeedAsset {
    let directory = bundle.join("catalog");
    need(must(ordinary(&directory), "Reviewed catalog directory unavailable").is_dir(),
         "Reviewed catalog path is not an ordinary directory");
    let path = directory.join("catalog.sqlite3");
    need(must(ordinary(&path), "Reviewed catalog file unavailable").is_file(),
         "Reviewed catalog path is not an ordinary file");
    need(must(path.canonicalize(), "Reviewed catalog path unavailable").starts_with(bundle),
         "Reviewed catalog escaped resources");
    let connection = must(Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY),
                          "Reviewed catalog metadata could not be opened read-only");
    must(connection.execute_batch("PRAGMA query_only=ON; PRAGMA trusted_schema=OFF;"),
         "Reviewed catalog metadata hardening failed");
    // length(BLOB) reads its size; the seed attachment bodies never enter a Vec.
    let (id, sha256, size): (String, String, i64) = must(connection.query_row(
        "SELECT asset_id,sha256,length(bytes) FROM catalog_asset_bytes \
         ORDER BY length(bytes) DESC,asset_id LIMIT 1", [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    ), "Reviewed catalog has no usable attachment metadata");
    need(!id.is_empty() && id.len() <= 255
         && id.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte)),
         "Reviewed catalog attachment identity invalid");
    need(sha256.len() == 64 && sha256.bytes().all(|byte|
         byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)), "Reviewed catalog attachment checksum invalid");
    need(size > 0 && size <= MAX_ASSET_RESPONSE_BYTES as i64,
         "Reviewed catalog largest attachment exceeds the decoded response bound");
    SeedAsset { id, sha256, size: size as usize }
}

fn verify_seed_asset_download(service: &Running, expected: &SeedAsset) {
    let response = must(service.service().request(backend::ApiRequest {
        method: "GET".into(), path: format!("/api/v1/assets/{}/download", expected.id),
        body: None, headers: BTreeMap::new(),
    }), "Packaged largest seed attachment request failed");
    need(response.status == 200, "Packaged largest seed attachment did not return success");
    let bytes = decode_base64(&response.body_base64, MAX_ASSET_RESPONSE_BYTES)
        .unwrap_or_else(|| panic!("Packaged largest seed attachment encoding invalid"));
    need(bytes.len() == expected.size, "Packaged largest seed attachment size mismatch");
    need(must(platform::sha256(&bytes), "Packaged largest seed attachment checksum failed") == expected.sha256,
         "Packaged largest seed attachment checksum mismatch");
}
fn request(service: &Running, method: &str, body: Option<Value>) -> Value {
    // Fixed allowlisted route. No generic API/capabilities/AI endpoint is accepted.
    let response = must(service.service().request(backend::ApiRequest {
        method: method.into(), path: STATE_PATH.into(), body: body.map(|v| v.to_string()),
        headers: BTreeMap::new(),
    }), "Packaged private request failed");
    need(response.status == 200, "Packaged client-state route did not return success");
    let bytes = decode_json_base64(&response.body_base64)
        .unwrap_or_else(|| panic!("Packaged client-state response encoding invalid"));
    must(serde_json::from_slice(&bytes), "Packaged client-state JSON invalid")
}
fn expected(version: u64, enabled: Option<bool>) -> Value {
    let mut entries = serde_json::Map::new();
    if let Some(value) = enabled { entries.insert(STATE_KEY.into(), json!(value)); }
    json!({"version":version,"value":{"version":1,"entries":entries}})
}
fn put(service: &Running, previous: u64, enabled: bool) -> Value {
    let value = expected(previous + 1, Some(enabled))["value"].clone();
    request(service, "PUT", Some(json!({"expectedVersion":previous,"value":value})))
}
fn flush(service: &Running, label: &'static str, timings: &mut BTreeMap<&'static str, u128>) {
    let began = Instant::now();
    let proof = must(service.service().call("flush", json!({})), "Packaged durable flush failed");
    need(proof.get("committed").and_then(Value::as_bool) == Some(true)
         && proof.get("pendingMutations").and_then(Value::as_u64) == Some(0), "Packaged durable flush proof invalid");
    timings.insert(label, began.elapsed().as_millis());
}
fn call(service: &Running, operation: &str, payload: Value) -> Value {
    need(["export_bundle", "import_preview", "import_bundle"].contains(&operation), "TEST_ONLY transfer operation refused");
    must(service.service().call(operation, payload), "Packaged full transfer operation failed")
}
fn digest(value: &Value) -> String {
    let digest = value.get("contentSha256").and_then(Value::as_str)
        .unwrap_or_else(|| panic!("Packaged transfer digest absent"));
    need(digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
         "Packaged transfer digest invalid");
    digest.into()
}

#[test]
fn response_decoder_preserves_state_bound_and_strict_asset_bound() {
    need(decode_base64("Zm9v", 3) == Some(b"foo".to_vec()), "TEST_ONLY base64 known vector failed");
    need(decode_base64("Zm9v", 2).is_none(), "TEST_ONLY base64 decoded bound failed");
    for invalid in ["Zg=a", "Zh==", "Zm9=", "Zg==Zg==", "Zg==\n"] {
        need(decode_base64(invalid, MAX_ASSET_RESPONSE_BYTES).is_none(),
             "TEST_ONLY noncanonical base64 accepted");
    }
    let over_state_bound = "AAAA".repeat(MAX_STATE_RESPONSE_BYTES / 3 + 1);
    need(decode_json_base64(&over_state_bound).is_none(), "TEST_ONLY JSON state bound weakened");
    need(decode_base64(&over_state_bound, MAX_ASSET_RESPONSE_BYTES).is_some(),
         "TEST_ONLY separate attachment response bound failed");
}

#[test]
#[ignore = "Requires ROOT GO, completed reviewed resources and an exclusive runtime lease"]
fn packaged_backend_runtime_roundtrip() {
    let bundle = resources(); // Refuse absent/unapproved resources before a fixture or process exists.
    need(decode_json_base64("eyJ2ZXJzaW9uIjowfQ==") == Some(br#"{"version":0}"#.to_vec())
         && decode_json_base64("Zg=a").is_none(), "TEST_ONLY base64 helper calibration failed");
    let largest_asset = largest_seed_asset(&bundle);
    let mut fixture = OwnedFixture::new();
    let profile_a = fixture.root.join("profile-a");
    let profile_b = fixture.root.join("profile-b");
    let absent_legacy = fixture.root.join("absent-legacy.sqlite3");
    let archive = fixture.root.join("synthetic-user-state.kontur");
    let mut timings = BTreeMap::new();

    let first = Running::start(&profile_a, &bundle, &absent_legacy, "coldStartup", &mut timings);
    need(request(&first, "GET", None) == expected(0, None), "Fresh TEST_ONLY profile contained unexpected user state");
    let began = Instant::now();
    verify_seed_asset_download(&first, &largest_asset);
    timings.insert("largestSeedAssetDownload", began.elapsed().as_millis());
    let began = Instant::now();
    need(put(&first, 0, true) == expected(1, Some(true)), "Synthetic state write acknowledgement mismatch");
    need(request(&first, "GET", None) == expected(1, Some(true)), "Synthetic state readback mismatch");
    timings.insert("writeReadRoundtrip", began.elapsed().as_millis());
    flush(&first, "firstFlush", &mut timings);
    first.stop();

    let warm = Running::start(&profile_a, &bundle, &absent_legacy, "warmStartup", &mut timings);
    need(request(&warm, "GET", None) == expected(1, Some(true)), "Synthetic state was not retained after shutdown/reopen");
    need(put(&warm, 1, false) == expected(2, Some(false)), "Synthetic persisted version did not advance");
    flush(&warm, "warmFlush", &mut timings);
    let began = Instant::now();
    let exported = call(&warm, "export_bundle", json!({"path":archive}));
    let exported_digest = digest(&exported);
    need(exported.get("format").and_then(Value::as_str) == Some("kontur-full-v1"), "Packaged export format mismatch");
    need(must(ordinary(&archive), "Packaged export file unavailable").is_file(), "Packaged export is not an ordinary file");
    timings.insert("fullExport", began.elapsed().as_millis());
    warm.stop();

    let target = Running::start(&profile_b, &bundle, &absent_legacy, "targetColdStartup", &mut timings);
    need(request(&target, "GET", None) == expected(0, None), "Fresh import target contained unexpected user state");
    let began = Instant::now();
    // Current documented Dispatcher schema: preview takes path; confirm takes expectedSha256.
    // Tauri's separate Settings/preview wrapper compatibility is not tested here.
    let preview = call(&target, "import_preview", json!({"path":archive}));
    need(preview.get("format").and_then(Value::as_str) == Some("kontur-full-v1"), "Packaged preview format mismatch");
    need(digest(&preview) == exported_digest, "Packaged preview did not bind the exported archive");
    need(preview.get("conflictCount").and_then(Value::as_u64) == Some(0), "Packaged preview reported an unexpected conflict");
    let imported = call(&target, "import_bundle", json!({"path":archive,"expectedSha256":exported_digest}));
    need(imported.get("format").and_then(Value::as_str) == Some("kontur-full-v1"), "Packaged import format mismatch");
    need(digest(&imported) == exported_digest, "Packaged import digest mismatch");
    need(request(&target, "GET", None) == expected(2, Some(false)), "Synthetic client state did not survive full transfer");
    timings.insert("fullPreviewImportRead", began.elapsed().as_millis());
    flush(&target, "targetFlush", &mut timings);
    target.stop();

    let reopened = Running::start(&profile_b, &bundle, &absent_legacy, "targetWarmStartup", &mut timings);
    need(request(&reopened, "GET", None) == expected(2, Some(false)), "Imported synthetic state was not durable on reopen");
    flush(&reopened, "finalFlush", &mut timings);
    reopened.stop();
    must(fixture.cleanup(), "TEST_ONLY verified fixture cleanup failed; fixture preserved when unsafe");
    // Fixed labels, booleans and timings only: never paths, task contents, code or private JSON.
    println!("KONTUR_PACKAGED_BACKEND_METADATA {}", json!({"schema":1,"scope":"synthetic-user-state",
        "transport":"production-rust-stdio-bundled-python-asgi-sqlite","timingsMs":timings,
        "clientStateRetained":true,"fullTransferRetained":true,"largestSeedAssetVerified":true,"ownedFixtureCleaned":true,
        "guiTested":false,"tauriInvokeTested":false,"realAiTested":false}));
}
