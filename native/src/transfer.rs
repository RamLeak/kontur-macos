//! Versioned, bounded, data-only SQLite transfer. Never execute imported SQL.
use crate::{domain::*, platform::{publish_noclobber,sha256}, storage::{self, Store}};
use rusqlite::{config::DbConfig, params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use std::{collections::{BTreeMap, BTreeSet}, fs::{self, File, OpenOptions}, io::{Read, Write}, path::{Path, PathBuf}, time::Duration};

pub const APPLICATION_ID: i64 = 0x4b4f4e54;
pub const MAX_BUNDLE_BYTES: u64 = 256 * 1024 * 1024;
pub const MAX_TASKS: usize = 50_000;
pub const MAX_ATTEMPTS: usize = 500_000;
pub const MAX_DOCUMENTS: usize = 10_000;
pub const MAX_ASSETS: usize = 10_000;
pub const MAX_ASSET_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_TOTAL_ASSET_BYTES: usize = 128 * 1024 * 1024;
pub const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_SMALL_BYTES: usize = 64 * 1024;
const MAX_SAFE_INTEGER: i64 = 9_007_199_254_740_991;
pub(crate) const MAX_TIMESTAMP_MS: i64 = 8_640_000_000_000_000;
const LEGACY_WARNINGS_KEY: &str = "kontur.legacy12.transferWarnings";

pub const SCHEMA_SQL: &str = r#"
CREATE TABLE metadata (key TEXT NOT NULL PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE assets (id TEXT NOT NULL PRIMARY KEY,name TEXT NOT NULL,mime TEXT NOT NULL,bytes BLOB NOT NULL,sha256 TEXT NOT NULL);
CREATE TABLE tasks (source_id TEXT NOT NULL,revision TEXT NOT NULL,type_number INTEGER NOT NULL,title TEXT NOT NULL,statement TEXT NOT NULL,blocks_json TEXT,asset_ids_json TEXT NOT NULL,PRIMARY KEY(source_id,revision));
CREATE TABLE task_heads (source_id TEXT NOT NULL PRIMARY KEY,revision TEXT NOT NULL);
CREATE TABLE attempts (id TEXT NOT NULL PRIMARY KEY,task_source_id TEXT NOT NULL,task_revision TEXT,verdict TEXT NOT NULL,source TEXT NOT NULL,created_at INTEGER NOT NULL,code TEXT NOT NULL,answer TEXT NOT NULL);
CREATE TABLE documents (id TEXT NOT NULL PRIMARY KEY,task_source_id TEXT,task_revision TEXT,title TEXT NOT NULL,type_number INTEGER,code TEXT NOT NULL,statement TEXT NOT NULL,board_json TEXT NOT NULL,revision INTEGER NOT NULL,updated_at INTEGER NOT NULL);
CREATE TABLE preferences (key TEXT NOT NULL PRIMARY KEY,value_json TEXT NOT NULL);
"#;

#[derive(Clone)]
pub(crate) struct BundleData {
    pub tasks: Vec<Task>,
    pub heads: BTreeMap<String, String>,
    pub attempts: Vec<Attempt>,
    pub documents: Vec<Document>,
    pub assets: Vec<AssetData>,
    pub preferences: Preferences,
}

struct ValidatedBundle { data: BundleData, digest: String }
struct TemporaryFile(PathBuf);
impl Drop for TemporaryFile { fn drop(&mut self) { let _ = fs::remove_file(&self.0); } }

struct ImportPlan {
    tasks: Vec<Task>, heads: Vec<(String, String)>, attempts: Vec<Attempt>, documents: Vec<Document>, assets: Vec<AssetData>,
    receipts: Vec<(String, String, String)>, preferences: Option<Preferences>, preview: ImportPreview,
}

impl ImportPlan {
    fn changes_data(&self) -> bool {
        !self.tasks.is_empty() || !self.heads.is_empty() || !self.attempts.is_empty() || !self.documents.is_empty() || !self.assets.is_empty() || !self.receipts.is_empty() || self.preferences.is_some()
    }
}

impl Store {
    pub fn preview_import(&self, path: impl AsRef<Path>) -> AppResult<ImportPreview> {
        let bundle = read_bundle(path.as_ref(), &self.root)?;
        Ok(plan_import(&self.connection, &bundle.data, &bundle.digest)?.preview)
    }

    pub fn import_bundle(&mut self, path: impl AsRef<Path>) -> AppResult<ImportResult> {
        self.import_validated(read_bundle(path.as_ref(), &self.root)?)
    }

    /// Validation and digest comparison use the same private copy; no path/hash TOCTOU.
    pub fn import_bundle_checked(&mut self, path: impl AsRef<Path>, expected_sha256: &str) -> AppResult<ImportResult> {
        let bundle = read_bundle(path.as_ref(), &self.root)?;
        if bundle.digest != expected_sha256 { return Err(AppError::new("import_changed", "Файл изменился после предварительного просмотра. Выберите его заново.")); }
        self.import_validated(bundle)
    }

    fn import_validated(&mut self, bundle: ValidatedBundle) -> AppResult<ImportResult> {
        let preliminary = plan_import(&self.connection, &bundle.data, &bundle.digest)?;
        if !preliminary.changes_data() { return Ok(import_result(&preliminary.preview, String::new())); }
        drop(preliminary);
        let backup = self.backup_before_import()?;
        let transaction = self.connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
        // Replan under the write lock in case another app instance committed meanwhile.
        let plan = plan_import(&transaction, &bundle.data, &bundle.digest)?;
        for asset in &plan.assets { storage::insert_asset(&transaction, asset)?; }
        for task in &plan.tasks { storage::insert_task(&transaction, task)?; }
        for (source_id, revision) in &plan.heads {
            transaction.execute("INSERT INTO task_heads(source_id,revision) VALUES (?1,?2) ON CONFLICT(source_id) DO UPDATE SET revision=excluded.revision", params![source_id, revision]).map_err(|_| storage_error())?;
        }
        for attempt in &plan.attempts { storage::insert_attempt(&transaction, attempt)?; }
        for document in &plan.documents { storage::insert_document(&transaction, document)?; }
        for (source, fingerprint, local_id) in &plan.receipts {
            transaction.execute("INSERT INTO import_receipts(source_id,fingerprint,local_id) VALUES (?1,?2,?3)", params![source, fingerprint, local_id]).map_err(|_| storage_error())?;
        }
        if let Some(preferences) = &plan.preferences { storage::write_preferences(&transaction, preferences)?; }
        transaction.commit().map_err(|_| storage_error())?;
        Ok(import_result(&plan.preview, backup.to_string_lossy().into_owned()))
    }

    fn backup_before_import(&self) -> AppResult<PathBuf> {
        let directory = self.root.join("backups");
        fs::create_dir_all(&directory).map_err(|_| backup_error())?;
        let path = directory.join(format!("{}.sqlite3", storage::fresh_id("pre-import")));
        if self.connection.backup("main", &path, None).is_err() {
            let _ = fs::remove_file(&path);
            return Err(backup_error());
        }
        if flush_file(&path).is_err() { let _ = fs::remove_file(&path); return Err(backup_error()); }
        Ok(path)
    }

    pub fn export_bundle(&self, path: impl AsRef<Path>) -> AppResult<ExportResult> {
        let path = path.as_ref();
        if path.exists() { return Err(destination_exists()); }
        let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).ok_or_else(io_error)?;
        if !parent.is_dir() { return Err(io_error()); }
        let read_transaction = self.connection.unchecked_transaction().map_err(|_| storage_error())?;
        preflight_export(&self.connection,MAX_BUNDLE_BYTES)?;
        let data = self.all_data()?;
        read_transaction.commit().map_err(|_| storage_error())?;
        validate_bundle_data(&data)?;
        let temporary = TemporaryFile(parent.join(format!(".{}.part", storage::fresh_id("kontur-export"))));
        let reservation = OpenOptions::new().create_new(true).write(true).open(&temporary.0).map_err(|_| io_error())?;
        drop(reservation);
        write_container(&temporary.0, &data)?;
        flush_file(&temporary.0)?;
        let bytes = bounded_bytes(&temporary.0)?;
        let digest = sha256(&bytes)?;
        // Native MoveFileExW WRITE_THROUGH publishes the complete sibling without
        // replacement and also supports FAT/exFAT destinations (no hard-link need).
        publish_noclobber(&temporary.0,path)?;
        Ok(ExportResult { path: path.to_string_lossy().into_owned(), content_sha256: digest, task_count: data.tasks.len() as u64, attempt_count: data.attempts.len() as u64, document_count: data.documents.len() as u64, asset_count: data.assets.len() as u64 })
    }
}

fn import_result(preview: &ImportPreview, backup_path: String) -> ImportResult {
    ImportResult { content_sha256: preview.content_sha256.clone(), imported_tasks: preview.new_tasks, imported_attempts: preview.new_attempts, imported_documents: preview.new_documents, imported_assets: preview.new_assets, document_conflicts: preview.document_conflicts, duplicate_records: preview.duplicate_records, backup_path }
}

fn read_bundle(path: &Path, root: &Path) -> AppResult<ValidatedBundle> {
    let bytes = bounded_bytes(path)?;
    if bytes.len() < 100 || !bytes.starts_with(b"SQLite format 3\0") || bytes[18] != 1 || bytes[19] != 1 { return Err(invalid_data()); }
    let digest = sha256(&bytes)?;
    let temporary = TemporaryFile(root.join(format!(".{}.sqlite3", storage::fresh_id("validate"))));
    {
        let mut file = OpenOptions::new().write(true).create_new(true).open(&temporary.0).map_err(|_| io_error())?;
        file.write_all(&bytes).map_err(|_| io_error())?;
    }
    drop(bytes);
    let connection = Connection::open_with_flags(&temporary.0, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX).map_err(|_| invalid_data())?;
    harden_reader(&connection)?;
    // No constraint code is allowed. Check the exact data-only DDL before running
    // integrity checking, which can otherwise evaluate attacker-supplied CHECKs.
    connection.execute_batch("PRAGMA ignore_check_constraints=ON;").map_err(|_| invalid_data())?;
    validate_schema_sql(&connection)?;
    let integrity: String = connection.query_row("PRAGMA integrity_check(1)", [], |r| r.get(0)).map_err(|_| invalid_data())?;
    if integrity != "ok" { return Err(invalid_data()); }
    let app: i64 = connection.pragma_query_value(None, "application_id", |r| r.get(0)).map_err(|_| invalid_data())?;
    let version: i64 = connection.pragma_query_value(None, "user_version", |r| r.get(0)).map_err(|_| invalid_data())?;
    if app != APPLICATION_ID || version != 1 { return Err(AppError::new("unsupported_format", "Версия файла .kontur не поддерживается.")); }
    validate_schema(&connection)?;
    validate_sql_limits(&connection)?;
    validate_metadata(&connection)?;
    let mut heads = BTreeMap::new();
    {
        let mut statement = connection.prepare("SELECT source_id,revision FROM task_heads ORDER BY source_id").map_err(|_| invalid_data())?;
        for row in statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).map_err(|_| invalid_data())? { let (id, revision) = row.map_err(|_| invalid_data())?; heads.insert(id, revision); }
    }
    let mut assets = Vec::new();
    for metadata in storage::read_asset_metadata(&connection).map_err(|_| invalid_data())? { assets.push(storage::find_asset(&connection, &metadata.id).map_err(|_| invalid_data())?.ok_or_else(invalid_data)?); }
    let data = BundleData { tasks: storage::read_tasks(&connection, false).map_err(|_| invalid_data())?, heads, attempts: storage::read_attempts(&connection).map_err(|_| invalid_data())?, documents: storage::read_documents(&connection).map_err(|_| invalid_data())?, assets, preferences: storage::read_preferences(&connection).map_err(|_| invalid_data())? };
    validate_bundle_data(&data)?;
    drop(connection);
    drop(temporary);
    Ok(ValidatedBundle { data, digest })
}

fn bounded_bytes(path: &Path) -> AppResult<Vec<u8>> {
    let file = File::open(path).map_err(|_| io_error())?;
    let metadata = file.metadata().map_err(|_| io_error())?;
    if !metadata.is_file() || metadata.len() > MAX_BUNDLE_BYTES { return Err(limit_error()); }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_BUNDLE_BYTES + 1).read_to_end(&mut bytes).map_err(|_| io_error())?;
    if bytes.len() as u64 > MAX_BUNDLE_BYTES { return Err(limit_error()); }
    Ok(bytes)
}

fn harden_reader(connection: &Connection) -> AppResult<()> {
    connection.busy_timeout(Duration::from_secs(2)).map_err(|_| invalid_data())?;
    let trusted = connection.set_db_config(DbConfig::SQLITE_DBCONFIG_TRUSTED_SCHEMA, false).map_err(|_| invalid_data())?;
    if trusted { return Err(invalid_data()); }
    // rusqlite deliberately has no ENABLE_LOAD_EXTENSION DbConfig variant. The C API
    // disables BOTH SQL and C extension loading; handle lives for this call only.
    let result = unsafe {
        let handle = connection.handle();
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_ATTACHED, 0);
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_COLUMN, 64);
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_EXPR_DEPTH, 64);
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_VDBE_OP, 100_000);
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_FUNCTION_ARG, 32);
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_SQL_LENGTH, 100_000);
        rusqlite::ffi::sqlite3_limit(handle, rusqlite::ffi::SQLITE_LIMIT_LENGTH, (MAX_ASSET_BYTES + MAX_BODY_BYTES) as i32);
        rusqlite::ffi::sqlite3_enable_load_extension(handle, 0)
    };
    if result != rusqlite::ffi::SQLITE_OK { return Err(invalid_data()); }
    Ok(())
}

// (name, declared type, NOT NULL, primary-key position); all entries are static.
type Column = (&'static str, &'static str, bool, i64);
const TABLES: &[(&str, &[Column], usize)] = &[
    ("metadata", &[("key","TEXT",true,1),("value","TEXT",true,0)], 4),
    ("assets", &[("id","TEXT",true,1),("name","TEXT",true,0),("mime","TEXT",true,0),("bytes","BLOB",true,0),("sha256","TEXT",true,0)], MAX_ASSETS),
    ("tasks", &[("source_id","TEXT",true,1),("revision","TEXT",true,2),("type_number","INTEGER",true,0),("title","TEXT",true,0),("statement","TEXT",true,0),("blocks_json","TEXT",false,0),("asset_ids_json","TEXT",true,0)], MAX_TASKS),
    ("task_heads", &[("source_id","TEXT",true,1),("revision","TEXT",true,0)], MAX_TASKS),
    ("attempts", &[("id","TEXT",true,1),("task_source_id","TEXT",true,0),("task_revision","TEXT",false,0),("verdict","TEXT",true,0),("source","TEXT",true,0),("created_at","INTEGER",true,0),("code","TEXT",true,0),("answer","TEXT",true,0)], MAX_ATTEMPTS),
    ("documents", &[("id","TEXT",true,1),("task_source_id","TEXT",false,0),("task_revision","TEXT",false,0),("title","TEXT",true,0),("type_number","INTEGER",false,0),("code","TEXT",true,0),("statement","TEXT",true,0),("board_json","TEXT",true,0),("revision","INTEGER",true,0),("updated_at","INTEGER",true,0)], MAX_DOCUMENTS),
    ("preferences", &[("key","TEXT",true,1),("value_json","TEXT",true,0)], 100),
];

fn validate_schema_sql(connection: &Connection) -> AppResult<()> {
    let mut permitted = BTreeMap::new();
    for declaration in SCHEMA_SQL.split(';').filter(|s| !s.trim().is_empty()) {
        let tokens = ddl_tokens(declaration)?;
        let name = tokens.get(2).ok_or_else(invalid_data)?.clone();
        permitted.insert(name,tokens);
    }
    let mut found = BTreeSet::new();
    let mut statement = connection.prepare("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY name LIMIT 32").map_err(|_| invalid_data())?;
    let rows = statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, Option<String>>(3)?))).map_err(|_| invalid_data())?;
    for row in rows {
        let (kind, name, table, sql) = row.map_err(|_| invalid_data())?;
        if kind == "index" && sql.is_none() && name.starts_with("sqlite_autoindex_") && TABLES.iter().any(|(t,_,_)| *t == table) { continue; }
        if kind != "table" || name != table || !TABLES.iter().any(|(t,_,_)| *t == name) { return Err(invalid_data()); }
        let actual = ddl_tokens(sql.as_deref().ok_or_else(invalid_data)?)?;
        if permitted.get(&name) != Some(&actual) || !found.insert(name) { return Err(invalid_data()); }
    }
    if found.len() != TABLES.len() { return Err(invalid_data()); }
    let object_count: i64 = connection.query_row("SELECT count(*) FROM sqlite_schema", [], |r| r.get(0)).map_err(|_| invalid_data())?;
    if object_count > 14 { return Err(invalid_data()); }
    Ok(())
}

/// The permitted grammar has only ASCII words and (),; punctuation. There are
/// no literals, comments, quotes, expressions or optional schema clauses. Token
/// comparison normalizes only ASCII case/whitespace, preserving SQL structure.
fn ddl_tokens(sql: &str) -> AppResult<Vec<String>> {
    if sql.len() > 16_384 || !sql.is_ascii() { return Err(invalid_data()); }
    let mut tokens = Vec::new(); let mut word = String::new(); let mut ended = false;
    for byte in sql.bytes() {
        if byte.is_ascii_alphanumeric() || byte == b'_' {
            if ended { return Err(invalid_data()); }
            word.push((byte as char).to_ascii_lowercase());
        } else {
            if !word.is_empty() { tokens.push(std::mem::take(&mut word)); }
            if byte.is_ascii_whitespace() { continue; }
            if ended { return Err(invalid_data()); }
            match byte { b'(' | b')' | b',' => tokens.push((byte as char).to_string()), b';' => ended = true, _ => return Err(invalid_data()) }
        }
    }
    if !word.is_empty() { tokens.push(word); }
    Ok(tokens)
}

fn validate_schema(connection: &Connection) -> AppResult<()> {
    for (table, expected, _) in TABLES {
        let mut statement = connection.prepare(&format!("PRAGMA table_xinfo({table})")).map_err(|_| invalid_data())?;
        let columns = statement.query_map([], |r| Ok((r.get::<_, i64>(0)?,r.get::<_, String>(1)?,r.get::<_, String>(2)?,r.get::<_, i64>(3)?,r.get::<_, Option<String>>(4)?,r.get::<_, i64>(5)?,r.get::<_, i64>(6)?))).map_err(|_| invalid_data())?.collect::<rusqlite::Result<Vec<_>>>().map_err(|_| invalid_data())?;
        if columns.len() != expected.len() { return Err(invalid_data()); }
        for (index, (actual, wanted)) in columns.iter().zip(expected.iter()).enumerate() {
            if actual.0 != index as i64 || actual.1 != wanted.0 || actual.2 != wanted.1 || actual.3 != i64::from(wanted.2) || actual.4.is_some() || actual.5 != wanted.3 || actual.6 != 0 { return Err(invalid_data()); }
        }
    }
    Ok(())
}

fn validate_sql_limits(connection: &Connection) -> AppResult<()> {
    for (table, columns, maximum) in TABLES {
        let count: i64 = connection.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0)).map_err(|_| invalid_data())?;
        if count < 0 || count as usize > *maximum { return Err(limit_error()); }
        let mut checks = Vec::new();
        for (name, kind, required, _) in *columns {
            let runtime = match *kind { "TEXT" => "text", "INTEGER" => "integer", "BLOB" => "blob", _ => return Err(invalid_data()) };
            checks.push(format!("typeof({name}) NOT IN ('{runtime}'{})", if *required { "" } else { ",'null'" }));
            let maximum = match (*table, *name) {
                ("assets", "bytes") => MAX_ASSET_BYTES,
                ("assets", "name") | (_, "title") => 512,
                ("assets", "mime") => 128,
                ("assets", "sha256") => 64,
                (_, "statement" | "code" | "blocks_json" | "board_json" | "asset_ids_json") => MAX_BODY_BYTES,
                (_, "answer" | "value_json") => MAX_SMALL_BYTES,
                _ => 256,
            };
            if *kind != "INTEGER" { checks.push(format!("length(CAST({name} AS BLOB))>{maximum}")); }
        }
        let invalid: bool = connection.query_row(&format!("SELECT EXISTS(SELECT 1 FROM {table} WHERE {} LIMIT 1)", checks.join(" OR ")), [], |r| r.get(0)).map_err(|_| invalid_data())?;
        if invalid { return Err(invalid_data()); }
    }
    let total: i64 = connection.query_row("SELECT coalesce(sum(length(bytes)),0) FROM assets", [], |r| r.get(0)).map_err(|_| invalid_data())?;
    if total < 0 || total as usize > MAX_TOTAL_ASSET_BYTES { return Err(limit_error()); }
    Ok(())
}

fn validate_metadata(connection: &Connection) -> AppResult<()> {
    let mut values = BTreeMap::new();
    let mut statement = connection.prepare("SELECT key,value FROM metadata").map_err(|_| invalid_data())?;
    for row in statement.query_map([], |r| Ok((r.get::<_, String>(0)?,r.get::<_, String>(1)?))).map_err(|_| invalid_data())? { let (key, value) = row.map_err(|_| invalid_data())?; values.insert(key, value); }
    if values.get("format").map(String::as_str) != Some("kontur-transfer-v1") || values.get("schemaVersion").map(String::as_str) != Some("1") { return Err(invalid_data()); }
    for (key, value) in values { match key.as_str() { "format" | "schemaVersion" => {}, "producer" => { text(&value, 256, true)?; }, "createdAt" => { let time = value.parse::<i64>().map_err(|_| invalid_data())?; timestamp(time)?; }, _ => return Err(invalid_data()) } }
    Ok(())
}

fn validate_bundle_data(data: &BundleData) -> AppResult<()> {
    if data.tasks.len() > MAX_TASKS || data.heads.len() > MAX_TASKS || data.attempts.len() > MAX_ATTEMPTS || data.documents.len() > MAX_DOCUMENTS || data.assets.len() > MAX_ASSETS { return Err(limit_error()); }
    let mut assets = BTreeSet::new(); let mut total_assets = 0usize;
    for asset in &data.assets {
        validate_asset(asset)?;
        total_assets = total_assets.checked_add(asset.bytes.len()).ok_or_else(limit_error)?;
        if !assets.insert(asset.metadata.id.as_str()) { return Err(invalid_data()); }
    }
    if total_assets > MAX_TOTAL_ASSET_BYTES { return Err(limit_error()); }
    let mut tasks = BTreeSet::new(); let mut sources = BTreeSet::new();
    for task in &data.tasks {
        identifier(&task.source_id)?; identifier(&task.revision)?; type_number(Some(task.type_number))?; text(&task.title,512,true)?; text(&task.statement,MAX_BODY_BYTES,false)?;
        if let Some(blocks) = &task.blocks { if !blocks.is_object() && !blocks.is_array() { return Err(invalid_data()); } json_limit(blocks,MAX_BODY_BYTES)?; }
        json_limit(&task.asset_ids,MAX_BODY_BYTES)?;
        let mut references = BTreeSet::new();
        if task.asset_ids.len() > MAX_ASSETS { return Err(limit_error()); }
        for id in &task.asset_ids { identifier(id)?; if !assets.contains(id.as_str()) || !references.insert(id) { return Err(invalid_data()); } }
        if !tasks.insert((task.source_id.as_str(), task.revision.as_str())) { return Err(invalid_data()); }
        sources.insert(task.source_id.as_str());
    }
    if sources.len() != data.heads.len() { return Err(invalid_data()); }
    for (id, revision) in &data.heads { if !tasks.contains(&(id.as_str(),revision.as_str())) { return Err(invalid_data()); } }
    let mut attempt_ids = BTreeSet::new();
    for attempt in &data.attempts {
        validate_attempt(attempt)?;
        let valid_reference = match &attempt.task_revision {
            Some(revision) => tasks.contains(&(attempt.task_source_id.as_str(),revision.as_str())),
            None => sources.contains(attempt.task_source_id.as_str()),
        };
        if !valid_reference || !attempt_ids.insert(&attempt.id) { return Err(invalid_data()); }
    }
    let mut documents = BTreeSet::new();
    for document in &data.documents {
        validate_document(document)?;
        if !documents.insert(document.id.as_str()) { return Err(invalid_data()); }
        if let (Some(id),Some(revision)) = (&document.task_source_id,&document.task_revision) { if !tasks.contains(&(id.as_str(),revision.as_str())) { return Err(invalid_data()); } }
    }
    validate_preferences(&data.preferences)?;
    if let Some(id) = &data.preferences.selected_document_id { if !documents.contains(id.as_str()) { return Err(invalid_data()); } }
    Ok(())
}

pub(crate) fn validate_document(document: &Document) -> AppResult<()> {
    identifier(&document.id)?; text(&document.title,512,true)?; type_number(document.type_number)?; text(&document.code,MAX_BODY_BYTES,false)?; text(&document.statement,MAX_BODY_BYTES,false)?;
    if !document.board.is_object() { return Err(invalid_data()); }
    json_limit(&document.board,MAX_BODY_BYTES)?; timestamp(document.updated_at)?;
    if !(1..=MAX_SAFE_INTEGER).contains(&document.revision) { return Err(invalid_data()); }
    match (&document.task_source_id,&document.task_revision) { (None,None) if document.is_own_work => {}, (Some(id),Some(revision)) if !document.is_own_work => { identifier(id)?; identifier(revision)?; }, _ => return Err(invalid_data()) }
    Ok(())
}

pub(crate) fn validate_attempt(attempt: &Attempt) -> AppResult<()> {
    identifier(&attempt.id)?; identifier(&attempt.task_source_id)?; if let Some(revision) = &attempt.task_revision { identifier(revision)?; } timestamp(attempt.created_at)?; text(&attempt.code,MAX_BODY_BYTES,false)?; text(&attempt.answer,MAX_SMALL_BYTES,false)
}

pub(crate) fn validate_asset(asset: &AssetData) -> AppResult<()> {
    let metadata = &asset.metadata;
    identifier(&metadata.id)?; text(&metadata.name,512,true)?;
    if metadata.name == "." || metadata.name == ".." || metadata.name.chars().any(|c| c.is_control() || matches!(c,'/'|'\\'|':')) { return Err(invalid_data()); }
    let (major, minor) = metadata.mime.split_once('/').ok_or_else(invalid_data)?;
    let mime_token = |s: &str| !s.is_empty() && s.bytes().all(|c| c.is_ascii_alphanumeric() || b"!#$&^_.+-".contains(&c));
    if metadata.mime.len() > 128 || !mime_token(major) || !mime_token(minor) { return Err(invalid_data()); }
    if asset.bytes.len() > MAX_ASSET_BYTES || metadata.size_bytes != asset.bytes.len() as u64 { return Err(limit_error()); }
    if metadata.sha256.len() != 64 || !metadata.sha256.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)) || sha256(&asset.bytes)? != metadata.sha256 { return Err(AppError::new("checksum_mismatch", "Контрольная сумма вложения не совпадает.")); }
    Ok(())
}

pub(crate) fn validate_preferences(preferences: &Preferences) -> AppResult<()> {
    if preferences.values.len() + 1 > 100 { return Err(limit_error()); }
    if let Some(id) = &preferences.selected_document_id { identifier(id)?; }
    for (key,value) in &preferences.values {
        identifier(key)?;
        if key == "selectedDocumentId" { return Err(invalid_data()); }
        json_limit(value,MAX_SMALL_BYTES)?;
        if key == LEGACY_WARNINGS_KEY { let _ = legacy_warning_codes(value)?; }
    }
    Ok(())
}

fn legacy_warning_codes(value: &serde_json::Value) -> AppResult<BTreeSet<&str>> {
    let mut codes = BTreeSet::new();
    for code in value.as_array().ok_or_else(invalid_data)? {
        let code = code.as_str().ok_or_else(invalid_data)?;
        identifier(code)?;
        codes.insert(code);
    }
    Ok(codes)
}

fn identifier(value: &str) -> AppResult<()> { text(value,256,true)?; if value.chars().any(char::is_control) { return Err(invalid_data()); } Ok(()) }
fn text(value: &str, maximum: usize, nonempty: bool) -> AppResult<()> { if value.len() > maximum { return Err(limit_error()); } if value.contains('\0') || (nonempty && value.is_empty()) { return Err(invalid_data()); } Ok(()) }
fn type_number(value: Option<u8>) -> AppResult<()> { if value.is_some_and(|n| !(1..=27).contains(&n)) { return Err(invalid_data()); } Ok(()) }
fn timestamp(value: i64) -> AppResult<()> { if !(0..=MAX_TIMESTAMP_MS).contains(&value) { return Err(invalid_data()); } Ok(()) }
fn json_limit(value: &impl serde::Serialize, maximum: usize) -> AppResult<()> { if storage::json(value)?.len() > maximum { return Err(limit_error()); } Ok(()) }
pub(crate) fn limit_error() -> AppError { AppError::new("limit_exceeded", "Размер данных превышает допустимый предел файла Контур.") }
fn backup_error() -> AppError { AppError::new("backup_failed", "Не удалось создать резервную копию. Данные не импортированы.") }
fn destination_exists() -> AppError { AppError::new("destination_exists", "Файл уже существует. Выберите новое имя для экспорта.") }
fn flush_file(path: &Path) -> AppResult<()> { OpenOptions::new().read(true).write(true).open(path).and_then(|file| file.sync_all()).map_err(|_| io_error()) }

fn plan_import(connection: &Connection, data: &BundleData, digest: &str) -> AppResult<ImportPlan> {
    let local_tasks: BTreeMap<_,_> = storage::read_tasks(connection,false)?.into_iter().map(|t| ((t.source_id.clone(),t.revision.clone()),t)).collect();
    let local_attempts: BTreeMap<_,_> = storage::read_attempts(connection)?.into_iter().map(|a| (a.id.clone(),a)).collect();
    let mut local_documents: BTreeMap<_,_> = storage::read_documents(connection)?.into_iter().map(|d| (d.id.clone(),d)).collect();
    let local_assets: BTreeMap<_,_> = storage::read_asset_metadata(connection)?.into_iter().map(|a| (a.id.clone(),a)).collect();
    let mut plan = ImportPlan { tasks: Vec::new(), heads: Vec::new(), attempts: Vec::new(), documents: Vec::new(), assets: Vec::new(), receipts: Vec::new(), preferences: None, preview: ImportPreview { format_version: 1, content_sha256: digest.into(), task_count: data.tasks.len() as u64, attempt_count: data.attempts.len() as u64, document_count: data.documents.len() as u64, asset_count: data.assets.len() as u64, new_tasks: 0, new_attempts: 0, new_documents: 0, new_assets: 0, document_conflicts: 0, duplicate_records: 0 } };
    for task in &data.tasks {
        if let Some(local) = local_tasks.get(&(task.source_id.clone(),task.revision.clone())) { if local != task { return Err(storage::record_conflict()); } plan.preview.duplicate_records += 1; } else { plan.tasks.push(task.clone()); }
    }
    for (id,revision) in &data.heads {
        // An already-known historical revision cannot roll a later import back.
        if !local_tasks.contains_key(&(id.clone(),revision.clone())) { plan.heads.push((id.clone(),revision.clone())); }
    }
    for attempt in &data.attempts {
        if let Some(local) = local_attempts.get(&attempt.id) { if local != attempt { return Err(storage::record_conflict()); } plan.preview.duplicate_records += 1; } else { plan.attempts.push(attempt.clone()); }
    }
    for asset in &data.assets {
        if let Some(local) = local_assets.get(&asset.metadata.id) { if local != &asset.metadata { return Err(storage::record_conflict()); } plan.preview.duplicate_records += 1; } else { plan.assets.push(asset.clone()); }
    }
    let mut document_ids = BTreeMap::new();
    for document in &data.documents {
        let fingerprint = sha256(storage::json(document)?.as_bytes())?;
        let receipt: Option<String> = connection.query_row("SELECT local_id FROM import_receipts WHERE source_id=?1 AND fingerprint=?2", params![document.id, fingerprint], |r| r.get(0)).optional().map_err(|_| storage_error())?;
        if let Some(id) = receipt { if local_documents.contains_key(&id) { document_ids.insert(document.id.clone(),id); plan.preview.duplicate_records += 1; continue; } else { return Err(storage_error()); } }
        let target_id = if let Some(local) = local_documents.get(&document.id) {
            if local == document { plan.preview.duplicate_records += 1; document.id.clone() } else {
                let mut retained = document.clone(); retained.id = format!("doc-import-{fingerprint}");
                if let Some(local) = local_documents.get(&retained.id) { if local != &retained { return Err(storage::record_conflict()); } plan.preview.duplicate_records += 1; } else { local_documents.insert(retained.id.clone(),retained.clone()); plan.documents.push(retained.clone()); plan.preview.document_conflicts += 1; }
                retained.id
            }
        } else { local_documents.insert(document.id.clone(),document.clone()); plan.documents.push(document.clone()); document.id.clone() };
        plan.receipts.push((document.id.clone(),fingerprint,target_id.clone())); document_ids.insert(document.id.clone(),target_id);
    }
    let local_preferences = storage::read_preferences(connection)?;
    let mut merged = local_preferences.clone();
    for (key,value) in &data.preferences.values {
        if key == LEGACY_WARNINGS_KEY {
            let mut codes = legacy_warning_codes(value)?;
            if let Some(local) = merged.values.get(key) { codes.extend(legacy_warning_codes(local)?); }
            let union = serde_json::to_value(codes).map_err(|_| invalid_data())?;
            merged.values.insert(key.clone(),union);
        } else {
            merged.values.entry(key.clone()).or_insert_with(|| value.clone());
        }
    }
    if merged.selected_document_id.is_none() { if let Some(id) = &data.preferences.selected_document_id { merged.selected_document_id = document_ids.get(id).cloned(); } }
    validate_preferences(&merged)?;
    if merged != local_preferences { plan.preferences = Some(merged); }
    if local_tasks.len() + plan.tasks.len() > MAX_TASKS || local_attempts.len() + plan.attempts.len() > MAX_ATTEMPTS || local_documents.len() > MAX_DOCUMENTS || local_assets.len() + plan.assets.len() > MAX_ASSETS { return Err(limit_error()); }
    let total_bytes = local_assets.values().try_fold(0u64, |n,a| n.checked_add(a.size_bytes)).ok_or_else(limit_error)? + plan.assets.iter().map(|a| a.metadata.size_bytes).sum::<u64>();
    if total_bytes > MAX_TOTAL_ASSET_BYTES as u64 { return Err(limit_error()); }
    plan.preview.new_tasks = plan.tasks.len() as u64; plan.preview.new_attempts = plan.attempts.len() as u64; plan.preview.new_documents = plan.documents.len() as u64; plan.preview.new_assets = plan.assets.len() as u64;
    Ok(plan)
}

fn preflight_export(connection: &Connection, byte_budget: u64) -> AppResult<()> {
    let mut bytes = 0u64;
    for (table,columns,maximum) in TABLES {
        let count: i64 = connection.query_row(&format!("SELECT count(*) FROM {table}"),[],|r| r.get(0)).map_err(|_| storage_error())?;
        if count < 0 || count as usize > *maximum { return Err(limit_error()); }
        let lengths: Vec<_> = columns.iter().filter(|(_,kind,_,_)| *kind != "INTEGER").map(|(name,_,_,_)| format!("coalesce(length(CAST({name} AS BLOB)),0)")).collect();
        let total: i64 = connection.query_row(&format!("SELECT coalesce(sum({}),0) FROM {table}",lengths.join("+")),[],|r| r.get(0)).map_err(|_| storage_error())?;
        if total < 0 { return Err(storage_error()); }
        bytes = bytes.checked_add(total as u64).ok_or_else(limit_error)?;
        if bytes > byte_budget { return Err(limit_error()); }
    }
    let asset_bytes: i64 = connection.query_row("SELECT coalesce(sum(length(bytes)),0) FROM assets",[],|r| r.get(0)).map_err(|_| storage_error())?;
    if asset_bytes < 0 || asset_bytes as u64 > MAX_TOTAL_ASSET_BYTES as u64 { return Err(limit_error()); }
    Ok(())
}

fn write_container(path: &Path, data: &BundleData) -> AppResult<()> {
    write_container_limited(path,data,MAX_BUNDLE_BYTES)
}

// byte_budget has no frontend/API input; production always supplies the fixed
// format cap. Internal unit tests exercise the real SQLite bound with small files.
fn write_container_limited(path: &Path, data: &BundleData, byte_budget: u64) -> AppResult<()> {
    // The sibling already exists. Windows canonicalization supplies a verbatim
    // path; the MSVC SQLite VFS otherwise forwards a raw >MAX_PATH name to Win32.
    #[cfg(windows)]
    let sqlite_path = path.canonicalize().map_err(|_| io_error())?;
    #[cfg(not(windows))]
    let sqlite_path = path;
    let mut connection = Connection::open_with_flags(sqlite_path, OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX).map_err(|_| io_error())?;
    connection.execute_batch("PRAGMA page_size=4096;").map_err(|_| storage_error())?;
    let pages = byte_budget / 4096;
    if pages == 0 { return Err(limit_error()); }
    let maximum: i64 = connection.query_row(&format!("PRAGMA max_page_count={pages}"),[],|r| r.get(0)).map_err(|_| storage_error())?;
    let page_size: i64 = connection.pragma_query_value(None,"page_size",|r| r.get(0)).map_err(|_| storage_error())?;
    if maximum != pages as i64 || page_size != 4096 { return Err(storage_error()); }
    storage::configure_writer(&connection)?;
    connection.execute_batch(SCHEMA_SQL).map_err(|_| storage_error())?;
    connection.execute_batch("PRAGMA application_id=1263488596; PRAGMA user_version=1;").map_err(|_| storage_error())?;
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
    transaction.execute("INSERT INTO metadata(key,value) VALUES ('format','kontur-transfer-v1'),('schemaVersion','1'),('producer','Kontur Offline 0.1.0')", []).map_err(|_| storage_error())?;
    for asset in &data.assets { storage::insert_asset(&transaction,asset)?; }
    for task in &data.tasks { storage::insert_task(&transaction,task)?; }
    for (id,revision) in &data.heads { transaction.execute("INSERT INTO task_heads(source_id,revision) VALUES (?1,?2)", params![id,revision]).map_err(|_| storage_error())?; }
    for attempt in &data.attempts { storage::insert_attempt(&transaction,attempt)?; }
    for document in &data.documents { storage::insert_document(&transaction,document)?; }
    storage::write_preferences(&transaction,&data.preferences)?;
    transaction.commit().map_err(|_| storage_error())?;
    connection.close().map_err(|_| storage_error())?;
    Ok(())
}

#[cfg(test)]
mod export_limit_tests {
    use super::*;

    fn own_document(bytes: usize) -> Document {
        Document { id:"synthetic-own-document".into(),task_source_id:None,task_revision:None,title:"Synthetic".into(),type_number:None,code:"x".repeat(bytes),statement:String::new(),board:serde_json::json!({}),revision:1,updated_at:0,is_own_work:true }
    }

    #[test]
    fn cumulative_export_preflight_rejects_before_materializing_payloads() {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch(SCHEMA_SQL).unwrap();
        storage::insert_document(&connection,&own_document(5000)).unwrap();
        assert_eq!(preflight_export(&connection,4096).unwrap_err().code,"limit_exceeded");
        assert!(preflight_export(&connection,16_384).is_ok());
    }

    #[test]
    fn sqlite_page_limit_bounds_export_growth_and_temporary_cleanup() {
        let path = std::env::temp_dir().join(format!("{}.sqlite3",storage::fresh_id("kontur-synthetic-page-limit")));
        let temporary = TemporaryFile(path.clone());
        OpenOptions::new().write(true).create_new(true).open(&path).unwrap();
        let data = BundleData { tasks:Vec::new(),heads:BTreeMap::new(),attempts:Vec::new(),documents:vec![own_document(32*1024)],assets:Vec::new(),preferences:Preferences::default() };
        let budget = 16*4096;
        assert!(write_container_limited(&path,&data,budget).is_err());
        assert!(fs::metadata(&path).unwrap().len() <= budget);
        drop(temporary); assert!(!path.exists());
    }

    #[test]
    #[cfg(windows)]
    fn sqlite_writer_supports_long_cyrillic_windows_path() {
        use std::os::windows::ffi::OsStrExt;
        let root = std::env::temp_dir().join(storage::fresh_id("kontur-synthetic-long-writer"));
        let directory = root.join("д".repeat(110)).join("я".repeat(110)); fs::create_dir_all(&directory).unwrap();
        let path = directory.join(".container.part"); assert!(path.as_os_str().encode_wide().count() > 300);
        let temporary = TemporaryFile(path.clone()); OpenOptions::new().write(true).create_new(true).open(&path).unwrap();
        let data = BundleData { tasks:Vec::new(),heads:BTreeMap::new(),attempts:Vec::new(),documents:vec![own_document(8)],assets:Vec::new(),preferences:Preferences::default() };
        // This calls the real writer without the later native publication step.
        // Within this function file_error can only come from SQLite initial open.
        let result = write_container(&path,&data); drop(temporary);
        let target = root.canonicalize().unwrap(); let temp_root = std::env::temp_dir().canonicalize().unwrap();
        assert!(target.starts_with(&temp_root) && target.file_name().unwrap().to_string_lossy().starts_with("kontur-synthetic-long-writer-")); fs::remove_dir_all(&target).unwrap();
        assert!(result.is_ok(),"Long-path SQLite writer stage failed (file_error denotes initial open): {:?}",result.err());
    }
}
