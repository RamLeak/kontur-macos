//! SQLite is the authority; successful mutations acknowledge a FULL synchronous commit.
use crate::domain::*;
use crate::transfer::{self, BundleData};
use rusqlite::{params, Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use std::{collections::{BTreeMap,BTreeSet}, fs, path::{Path, PathBuf}, sync::atomic::{AtomicU64, Ordering}, time::{Duration, SystemTime, UNIX_EPOCH}};

pub(crate) const LOCAL_APPLICATION_ID: i64 = 0x4b4f4e4c;
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

pub struct Store {
    pub(crate) connection: Connection,
    pub(crate) root: PathBuf,
}

impl Store {
    pub fn open(root: impl AsRef<Path>) -> AppResult<Self> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(&root).map_err(|_| io_error())?;
        let path = root.join("kontur.sqlite3");
        let mut connection = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE | OpenFlags::SQLITE_OPEN_NO_MUTEX).map_err(|_| storage_error())?;
        configure_writer(&connection)?;
        let objects: i64 = connection.query_row("SELECT count(*) FROM sqlite_schema",[],|r| r.get(0)).map_err(|_| storage_error())?;
        let initial_app: i64 = connection.pragma_query_value(None,"application_id",|r| r.get(0)).map_err(|_| storage_error())?;
        let initial_version: i64 = connection.pragma_query_value(None,"user_version",|r| r.get(0)).map_err(|_| storage_error())?;
        if objects == 0 && initial_app == 0 && initial_version == 0 {
            let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
            transaction.execute_batch(transfer::SCHEMA_SQL).map_err(|_| storage_error())?;
            transaction.execute_batch("CREATE TABLE import_receipts (source_id TEXT NOT NULL, fingerprint TEXT NOT NULL, local_id TEXT NOT NULL, PRIMARY KEY(source_id,fingerprint)); PRAGMA application_id=1263488588; PRAGMA user_version=1;").map_err(|_| storage_error())?;
            transaction.execute("INSERT INTO metadata(key,value) VALUES ('format','kontur-local-v1'),('schemaVersion','1')", []).map_err(|_| storage_error())?;
            transaction.commit().map_err(|_| storage_error())?;
        }
        let application_id: i64 = connection.pragma_query_value(None, "application_id", |r| r.get(0)).map_err(|_| storage_error())?;
        let version: i64 = connection.pragma_query_value(None, "user_version", |r| r.get(0)).map_err(|_| storage_error())?;
        let format: String = connection.query_row("SELECT value FROM metadata WHERE key='format'", [], |r| r.get(0)).map_err(|_| storage_error())?;
        if application_id != LOCAL_APPLICATION_ID || version != 1 || format != "kontur-local-v1" {
            return Err(AppError::new("unsupported_database", "Версия локального хранилища не поддерживается."));
        }
        let integrity: String = connection.query_row("PRAGMA quick_check(1)", [], |r| r.get(0)).map_err(|_| storage_error())?;
        if integrity != "ok" { return Err(storage_error()); }
        Ok(Self { connection, root })
    }

    pub fn snapshot(&self) -> AppResult<Snapshot> {
        let read_transaction = self.connection.unchecked_transaction().map_err(|_| storage_error())?;
        let tasks = read_tasks(&self.connection, true)?;
        let attempts = read_attempts(&self.connection)?;
        let documents = read_documents(&self.connection)?;
        let mut by_revision: BTreeMap<(&str,&str),Vec<&Attempt>> = BTreeMap::new();
        for attempt in &attempts { if let Some(revision) = &attempt.task_revision { by_revision.entry((&attempt.task_source_id,revision)).or_default().push(attempt); } }
        let started: BTreeSet<(&str,&str)> = documents.iter().filter_map(|d| d.task_source_id.as_deref().zip(d.task_revision.as_deref())).collect();
        let progress = tasks.iter().map(|task| {
            let key = (task.source_id.as_str(),task.revision.as_str());
            progress_from_matches(task,by_revision.get(&key).into_iter().flatten().copied(),started.contains(&key))
        }).collect();
        let snapshot = Snapshot { tasks, documents, attempts, progress, assets: read_asset_metadata(&self.connection)?, preferences: read_preferences(&self.connection)? };
        read_transaction.commit().map_err(|_| storage_error())?;
        Ok(snapshot)
    }

    pub fn create_document(&mut self, input: CreateDocument) -> AppResult<Document> {
        let task = match (&input.task_source_id, &input.task_revision) {
            (None, None) => None,
            (Some(id), Some(revision)) => Some(find_task(&self.connection, id, revision)?.ok_or_else(not_found)?),
            _ => return Err(invalid_data()),
        };
        let document = Document {
            id: fresh_id("doc"), task_source_id: input.task_source_id, task_revision: input.task_revision,
            title: input.title.unwrap_or_else(|| task.as_ref().map(|t| t.title.clone()).unwrap_or_else(|| "Своя работа".into())),
            type_number: input.type_number.or_else(|| task.as_ref().map(|t| t.type_number)),
            code: String::new(), statement: task.as_ref().map(|t| t.statement.clone()).unwrap_or_default(), board: serde_json::json!({}), revision: 1, updated_at: now_ms(), is_own_work: task.is_none(),
        };
        transfer::validate_document(&document)?;
        let transaction = self.connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
        let document_count: i64 = transaction.query_row("SELECT count(*) FROM documents",[],|r| r.get(0)).map_err(|_| storage_error())?;
        if document_count >= transfer::MAX_DOCUMENTS as i64 { return Err(transfer::limit_error()); }
        insert_document(&transaction, &document)?;
        select_document(&transaction, &document.id)?;
        transaction.commit().map_err(|_| storage_error())?;
        Ok(document)
    }

    /// Loading a document also persists selection; native serializes access to Store.
    pub fn document(&mut self, id: &str) -> AppResult<Document> {
        let document = find_document(&self.connection, id)?.ok_or_else(not_found)?;
        let transaction = self.connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
        select_document(&transaction, id)?;
        transaction.commit().map_err(|_| storage_error())?;
        Ok(document)
    }

    pub fn save_document(&mut self, input: SaveDocument) -> AppResult<Document> {
        let transaction = self.connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
        let existing = find_document(&transaction, &input.document_id)?.ok_or_else(not_found)?;
        if input.expected_revision != existing.revision { return Err(AppError::conflict(existing.revision)); }
        let document = Document { title: input.title, type_number: input.type_number, code: input.code, statement: input.statement, board: input.board, revision: existing.revision.checked_add(1).ok_or_else(invalid_data)?, updated_at: now_ms(), ..existing };
        transfer::validate_document(&document)?;
        transaction.execute("UPDATE documents SET title=?1,type_number=?2,code=?3,statement=?4,board_json=?5,revision=?6,updated_at=?7 WHERE id=?8 AND revision=?9", params![document.title, document.type_number, document.code, document.statement, json(&document.board)?, document.revision, document.updated_at, document.id, input.expected_revision]).map_err(|_| storage_error())?;
        select_document(&transaction, &document.id)?;
        transaction.commit().map_err(|_| storage_error())?;
        Ok(document)
    }

    pub fn verify_document_revision(&self, id: &str, revision: i64) -> AppResult<()> {
        let saved: Option<i64> = self.connection.query_row("SELECT revision FROM documents WHERE id=?1", [id], |r| r.get(0)).optional().map_err(|_| storage_error())?;
        match saved { Some(current) if current == revision => Ok(()), Some(current) => Err(AppError::conflict(current)), None => Err(not_found()) }
    }

    pub fn mark_attempt(&mut self, input: MarkAttempt) -> AppResult<ProgressSummary> {
        let task = find_task(&self.connection, &input.task_source_id, &input.task_revision)?.ok_or_else(not_found)?;
        let id = input.id.unwrap_or_else(|| fresh_id("attempt"));
        let previous = find_attempt(&self.connection, &id)?;
        let attempt = Attempt { id, task_source_id: input.task_source_id, task_revision: Some(input.task_revision), verdict: input.verdict, source: input.source, created_at: input.created_at.unwrap_or_else(|| previous.as_ref().map(|a| a.created_at).unwrap_or_else(now_ms)), code: input.code, answer: input.answer };
        transfer::validate_attempt(&attempt)?;
        if let Some(previous) = previous {
            if previous != attempt { return Err(record_conflict()); }
        } else {
            let transaction = self.connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
            let attempt_count: i64 = transaction.query_row("SELECT count(*) FROM attempts",[],|r| r.get(0)).map_err(|_| storage_error())?;
            if attempt_count >= transfer::MAX_ATTEMPTS as i64 { return Err(transfer::limit_error()); }
            insert_attempt(&transaction, &attempt)?;
            transaction.commit().map_err(|_| storage_error())?;
        }
        let has_document: bool = self.connection.query_row("SELECT EXISTS(SELECT 1 FROM documents WHERE task_source_id=?1 AND task_revision=?2)",params![task.source_id,task.revision],|r| r.get(0)).map_err(|_| storage_error())?;
        Ok(progress_for(&task, &read_attempts(&self.connection)?, has_document))
    }

    pub fn asset(&self, id: &str) -> AppResult<AssetData> {
        let asset = find_asset(&self.connection, id)?.ok_or_else(not_found)?;
        transfer::validate_asset(&asset)?;
        Ok(asset)
    }

    pub fn save_preferences(&mut self, preferences: Preferences) -> AppResult<Preferences> {
        transfer::validate_preferences(&preferences)?;
        if let Some(id) = &preferences.selected_document_id { if find_document(&self.connection, id)?.is_none() { return Err(not_found()); } }
        let transaction = self.connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(|_| storage_error())?;
        transaction.execute("DELETE FROM preferences", []).map_err(|_| storage_error())?;
        write_preferences(&transaction, &preferences)?;
        transaction.commit().map_err(|_| storage_error())?;
        Ok(preferences)
    }

    pub(crate) fn all_data(&self) -> AppResult<BundleData> {
        let mut heads = BTreeMap::new();
        let mut statement = self.connection.prepare("SELECT source_id,revision FROM task_heads ORDER BY source_id").map_err(|_| storage_error())?;
        for row in statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).map_err(|_| storage_error())? {
            let (id, revision) = row.map_err(|_| storage_error())?; heads.insert(id, revision);
        }
        let mut assets = Vec::new();
        for metadata in read_asset_metadata(&self.connection)? { assets.push(find_asset(&self.connection, &metadata.id)?.ok_or_else(storage_error)?); }
        Ok(BundleData { tasks: read_tasks(&self.connection, false)?, heads, attempts: read_attempts(&self.connection)?, documents: read_documents(&self.connection)?, assets, preferences: read_preferences(&self.connection)? })
    }
}

pub(crate) fn configure_writer(connection: &Connection) -> AppResult<()> {
    connection.busy_timeout(Duration::from_secs(3)).map_err(|_| storage_error())?;
    connection.execute_batch("PRAGMA trusted_schema=OFF; PRAGMA foreign_keys=ON; PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;").map_err(|_| storage_error())?;
    let synchronous: i64 = connection.pragma_query_value(None, "synchronous", |r| r.get(0)).map_err(|_| storage_error())?;
    if synchronous != 2 { return Err(storage_error()); }
    Ok(())
}

pub(crate) fn fresh_id(prefix: &str) -> String {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos();
    format!("{prefix}-{nanos:x}-{:x}-{:x}", std::process::id(), NEXT_ID.fetch_add(1, Ordering::Relaxed))
}

pub(crate) fn now_ms() -> i64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis().min(transfer::MAX_TIMESTAMP_MS as u128) as i64 }
pub(crate) fn record_conflict() -> AppError { AppError::new("record_conflict", "Одинаковый идентификатор содержит разные данные. Импорт отменён.") }
pub(crate) fn not_found() -> AppError { AppError::new("not_found", "Запись не найдена в локальном хранилище.") }
pub(crate) fn json(value: &impl serde::Serialize) -> AppResult<String> { serde_json::to_string(value).map_err(|_| invalid_data()) }

pub(crate) fn read_tasks(connection: &Connection, current_only: bool) -> AppResult<Vec<Task>> {
    let sql = if current_only { "SELECT t.source_id,t.revision,t.type_number,t.title,t.statement,t.blocks_json,t.asset_ids_json FROM tasks t JOIN task_heads h ON h.source_id=t.source_id AND h.revision=t.revision ORDER BY t.type_number,t.source_id" } else { "SELECT source_id,revision,type_number,title,statement,blocks_json,asset_ids_json FROM tasks ORDER BY source_id,revision" };
    let mut statement = connection.prepare(sql).map_err(|_| storage_error())?;
    let rows = statement.query_map([], task_row).map_err(|_| storage_error())?;
    rows.map(|row| row.map_err(|_| storage_error())).collect()
}

fn task_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Task> {
    let blocks: Option<String> = row.get(5)?;
    Ok(Task { source_id: row.get(0)?, revision: row.get(1)?, type_number: row.get(2)?, title: row.get(3)?, statement: row.get(4)?, blocks: blocks.map(|s| decode_json(&s, 5)).transpose()?, asset_ids: decode_json(&row.get::<_, String>(6)?, 6)? })
}

pub(crate) fn find_task(connection: &Connection, id: &str, revision: &str) -> AppResult<Option<Task>> {
    connection.query_row("SELECT source_id,revision,type_number,title,statement,blocks_json,asset_ids_json FROM tasks WHERE source_id=?1 AND revision=?2", params![id, revision], task_row).optional().map_err(|_| storage_error())
}

pub(crate) fn insert_task(connection: &Connection, task: &Task) -> AppResult<()> {
    connection.execute("INSERT INTO tasks(source_id,revision,type_number,title,statement,blocks_json,asset_ids_json) VALUES (?1,?2,?3,?4,?5,?6,?7)", params![task.source_id, task.revision, task.type_number, task.title, task.statement, task.blocks.as_ref().map(json).transpose()?, json(&task.asset_ids)?]).map_err(|_| storage_error())?; Ok(())
}

fn document_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Document> {
    let task_source_id: Option<String> = row.get(1)?;
    Ok(Document { id: row.get(0)?, is_own_work: task_source_id.is_none(), task_source_id, task_revision: row.get(2)?, title: row.get(3)?, type_number: row.get(4)?, code: row.get(5)?, statement: row.get(6)?, board: decode_json(&row.get::<_, String>(7)?, 7)?, revision: row.get(8)?, updated_at: row.get(9)? })
}

pub(crate) fn read_documents(connection: &Connection) -> AppResult<Vec<Document>> {
    let mut statement = connection.prepare("SELECT id,task_source_id,task_revision,title,type_number,code,statement,board_json,revision,updated_at FROM documents ORDER BY updated_at DESC,id").map_err(|_| storage_error())?;
    let rows = statement.query_map([], document_row).map_err(|_| storage_error())?;
    rows.map(|row| row.map_err(|_| storage_error())).collect()
}

pub(crate) fn find_document(connection: &Connection, id: &str) -> AppResult<Option<Document>> {
    connection.query_row("SELECT id,task_source_id,task_revision,title,type_number,code,statement,board_json,revision,updated_at FROM documents WHERE id=?1", [id], document_row).optional().map_err(|_| storage_error())
}

pub(crate) fn insert_document(connection: &Connection, document: &Document) -> AppResult<()> {
    connection.execute("INSERT INTO documents(id,task_source_id,task_revision,title,type_number,code,statement,board_json,revision,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)", params![document.id, document.task_source_id, document.task_revision, document.title, document.type_number, document.code, document.statement, json(&document.board)?, document.revision, document.updated_at]).map_err(|_| storage_error())?; Ok(())
}

fn attempt_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Attempt> {
    let verdict: String = row.get(3)?; let source: String = row.get(4)?;
    Ok(Attempt { id: row.get(0)?, task_source_id: row.get(1)?, task_revision: row.get(2)?, verdict: Verdict::parse(&verdict).map_err(|e| conversion(3, e))?, source: AttemptSource::parse(&source).map_err(|e| conversion(4, e))?, created_at: row.get(5)?, code: row.get(6)?, answer: row.get(7)? })
}

pub(crate) fn read_attempts(connection: &Connection) -> AppResult<Vec<Attempt>> {
    let mut statement = connection.prepare("SELECT id,task_source_id,task_revision,verdict,source,created_at,code,answer FROM attempts ORDER BY created_at,id").map_err(|_| storage_error())?;
    let rows = statement.query_map([], attempt_row).map_err(|_| storage_error())?;
    rows.map(|row| row.map_err(|_| storage_error())).collect()
}

pub(crate) fn find_attempt(connection: &Connection, id: &str) -> AppResult<Option<Attempt>> {
    connection.query_row("SELECT id,task_source_id,task_revision,verdict,source,created_at,code,answer FROM attempts WHERE id=?1", [id], attempt_row).optional().map_err(|_| storage_error())
}

pub(crate) fn insert_attempt(connection: &Connection, attempt: &Attempt) -> AppResult<()> {
    connection.execute("INSERT INTO attempts(id,task_source_id,task_revision,verdict,source,created_at,code,answer) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)", params![attempt.id, attempt.task_source_id, attempt.task_revision, attempt.verdict.as_str(), attempt.source.as_str(), attempt.created_at, attempt.code, attempt.answer]).map_err(|_| storage_error())?; Ok(())
}

pub(crate) fn read_asset_metadata(connection: &Connection) -> AppResult<Vec<AssetMetadata>> {
    let mut statement = connection.prepare("SELECT id,name,mime,length(bytes),sha256 FROM assets ORDER BY id").map_err(|_| storage_error())?;
    let rows = statement.query_map([], |r| Ok(AssetMetadata { id: r.get(0)?, name: r.get(1)?, mime: r.get(2)?, size_bytes: read_asset_size(r)?, sha256: r.get(4)? })).map_err(|_| storage_error())?;
    rows.map(|row| row.map_err(|_| storage_error())).collect()
}

pub(crate) fn find_asset(connection: &Connection, id: &str) -> AppResult<Option<AssetData>> {
    connection.query_row("SELECT id,name,mime,length(bytes),sha256,bytes FROM assets WHERE id=?1", [id], |r| Ok(AssetData { metadata: AssetMetadata { id: r.get(0)?, name: r.get(1)?, mime: r.get(2)?, size_bytes: read_asset_size(r)?, sha256: r.get(4)? }, bytes: r.get(5)? })).optional().map_err(|_| storage_error())
}

fn read_asset_size(row: &rusqlite::Row<'_>) -> rusqlite::Result<u64> {
    let size: i64 = row.get(3)?;
    u64::try_from(size).map_err(|_| rusqlite::Error::FromSqlConversionFailure(3,rusqlite::types::Type::Integer,Box::new(invalid_data())))
}

pub(crate) fn insert_asset(connection: &Connection, asset: &AssetData) -> AppResult<()> {
    connection.execute("INSERT INTO assets(id,name,mime,bytes,sha256) VALUES (?1,?2,?3,?4,?5)", params![asset.metadata.id, asset.metadata.name, asset.metadata.mime, asset.bytes, asset.metadata.sha256]).map_err(|_| storage_error())?; Ok(())
}

pub(crate) fn read_preferences(connection: &Connection) -> AppResult<Preferences> {
    let mut preferences = Preferences::default();
    let mut statement = connection.prepare("SELECT key,value_json FROM preferences ORDER BY key").map_err(|_| storage_error())?;
    let rows = statement.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).map_err(|_| storage_error())?;
    for row in rows { let (key, value) = row.map_err(|_| storage_error())?; if key == "selectedDocumentId" { preferences.selected_document_id = serde_json::from_str(&value).map_err(|_| invalid_data())?; } else { preferences.values.insert(key, serde_json::from_str(&value).map_err(|_| invalid_data())?); } }
    Ok(preferences)
}

pub(crate) fn write_preferences(connection: &Connection, preferences: &Preferences) -> AppResult<()> {
    for (key, value) in &preferences.values { connection.execute("INSERT OR REPLACE INTO preferences(key,value_json) VALUES (?1,?2)", params![key, json(value)?]).map_err(|_| storage_error())?; }
    select_document_value(connection, preferences.selected_document_id.as_deref())
}

fn select_document(connection: &Connection, id: &str) -> AppResult<()> { select_document_value(connection, Some(id)) }
fn select_document_value(connection: &Connection, id: Option<&str>) -> AppResult<()> {
    connection.execute("INSERT OR REPLACE INTO preferences(key,value_json) VALUES ('selectedDocumentId',?1)", [json(&id)?]).map_err(|_| storage_error())?; Ok(())
}

pub(crate) fn progress_for(task: &Task, attempts: &[Attempt], has_document: bool) -> ProgressSummary {
    progress_from_matches(task,attempts.iter().filter(|a| a.task_source_id == task.source_id && a.task_revision.as_deref() == Some(task.revision.as_str())),has_document)
}

fn progress_from_matches<'a>(task: &Task, matching: impl Iterator<Item=&'a Attempt>, has_document: bool) -> ProgressSummary {
    let mut last: Option<&Attempt> = None; let mut last_evaluated: Option<&Attempt> = None; let mut ever_solved = false; let mut count = 0;
    for attempt in matching {
        count += 1; ever_solved |= attempt.verdict == Verdict::Correct;
        if last.map_or(true,|a| (attempt.created_at,&attempt.id) > (a.created_at,&a.id)) { last = Some(attempt); }
        if attempt.verdict != Verdict::Unverified && last_evaluated.map_or(true,|a| (attempt.created_at,&attempt.id) > (a.created_at,&a.id)) { last_evaluated = Some(attempt); }
    }
    let status = match last_evaluated.map(|a| a.verdict) {
        Some(Verdict::Correct) => ProgressStatus::Solved,
        Some(Verdict::Wrong | Verdict::Error) => ProgressStatus::Error,
        _ if count > 0 || has_document => ProgressStatus::InProgress,
        _ => ProgressStatus::NotStarted,
    };
    ProgressSummary { task_source_id: task.source_id.clone(), task_revision: task.revision.clone(), status, ever_solved, last_verdict: last.map(|a| a.verdict), last_attempt_id: last.map(|a| a.id.clone()), attempt_count: count, updated_at: last.map(|a| a.created_at) }
}

fn decode_json<T: serde::de::DeserializeOwned>(text: &str, column: usize) -> rusqlite::Result<T> { serde_json::from_str(text).map_err(|_| conversion(column, invalid_data())) }
fn conversion(column: usize, error: AppError) -> rusqlite::Error { rusqlite::Error::FromSqlConversionFailure(column, rusqlite::types::Type::Text, Box::new(error)) }

#[cfg(test)]
mod asset_size_tests {
    use super::*;

    #[test]
    fn rejects_negative_and_signed_overflow_sql_sizes() {
        let connection = Connection::open_in_memory().unwrap();
        assert!(connection.query_row("SELECT NULL,NULL,NULL,-1",[],read_asset_size).is_err());
        assert!(connection.query_row("SELECT NULL,NULL,NULL,9223372036854775808",[],read_asset_size).is_err());
    }

    #[test]
    fn preserves_nonnegative_signed_sql_sizes_without_wrapping() {
        let connection = Connection::open_in_memory().unwrap();
        assert_eq!(connection.query_row("SELECT NULL,NULL,NULL,0",[],read_asset_size).unwrap(),0);
        assert_eq!(connection.query_row("SELECT NULL,NULL,NULL,9223372036854775807",[],read_asset_size).unwrap(),9_223_372_036_854_775_807);
    }
}
