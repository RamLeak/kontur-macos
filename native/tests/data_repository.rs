//! Synthetic, real SQLite integration tests. Never use a user's live data.
use kontur_offline_lib::{domain::*, platform::sha256, storage::Store, transfer::{APPLICATION_ID, MAX_BODY_BYTES, SCHEMA_SQL}};
use rusqlite::{params, Connection, OpenFlags};
use serde_json::json;
use std::{fs, path::{Path, PathBuf}, sync::atomic::{AtomicU64, Ordering}, time::{SystemTime, UNIX_EPOCH}};

const TASK: &str = "synthetic:task:12";
const REV: &str = "synthetic-revision-1";
const WARNINGS: &str = "kontur.legacy12.transferWarnings";
static NEXT: AtomicU64 = AtomicU64::new(0);

struct Sandbox(PathBuf);
impl Sandbox {
    fn new() -> Self {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let path = std::env::temp_dir().join(format!("kontur-data-test-{}-{nanos}-{}",std::process::id(),NEXT.fetch_add(1,Ordering::Relaxed)));
        fs::create_dir(&path).unwrap(); Self(path)
    }
    fn at(&self, name: &str) -> PathBuf { self.0.join(name) }
    fn archive(&self, name: &str, customize: impl FnOnce(&Connection)) -> PathBuf {
        let path = self.at(name);
        let connection = Connection::open(&path).unwrap();
        connection.execute_batch(SCHEMA_SQL).unwrap();
        connection.pragma_update(None,"application_id",APPLICATION_ID).unwrap();
        connection.pragma_update(None,"user_version",1).unwrap();
        connection.execute_batch("BEGIN IMMEDIATE; INSERT INTO metadata VALUES ('format','kontur-transfer-v1'); INSERT INTO metadata VALUES ('schemaVersion','1');").unwrap();
        let bytes = b"Synthetic attachment only.\n";
        connection.execute("INSERT INTO assets VALUES (?1,?2,?3,?4,?5)",params!["synthetic-asset","example.txt","text/plain",bytes.as_slice(),sha256(bytes).unwrap()]).unwrap();
        connection.execute("INSERT INTO tasks VALUES (?1,?2,12,?3,?4,?5,?6)",params![TASK,REV,"Синтетический пример","Условие для проверки формата.","[{\"kind\":\"text\",\"text\":\"Synthetic\"}]","[\"synthetic-asset\"]"]).unwrap();
        connection.execute("INSERT INTO task_heads VALUES (?1,?2)",params![TASK,REV]).unwrap();
        connection.execute("INSERT INTO attempts VALUES ('synthetic-event',?1,?2,'correct','imported',10,'print(42)','42')",params![TASK,REV]).unwrap();
        connection.execute("INSERT INTO documents VALUES ('synthetic-document',?1,?2,'Черновик',12,'print(42)','Сохранённое условие','{\"version\":1,\"items\":[]}',1,20)",params![TASK,REV]).unwrap();
        connection.execute_batch("INSERT INTO preferences VALUES ('selectedDocumentId','\"synthetic-document\"'); INSERT INTO preferences VALUES ('ui.theme','\"dark\"'); INSERT INTO preferences VALUES ('legacy.bookmarks','[\"synthetic:task:12\"]'); COMMIT;").unwrap();
        customize(&connection);
        connection.close().unwrap();
        path
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        // Resolve and verify the exact synthetic target before recursive cleanup.
        if let (Ok(target),Ok(temp)) = (self.0.canonicalize(),std::env::temp_dir().canonicalize()) {
            if target.starts_with(&temp) && target.file_name().is_some_and(|n| n.to_string_lossy().starts_with("kontur-data-test-")) { let _ = fs::remove_dir_all(&target); }
        }
    }
}

fn save(document: &Document, code: &str) -> SaveDocument {
    SaveDocument { document_id: document.id.clone(), expected_revision: document.revision, title: document.title.clone(), type_number: document.type_number, code: code.into(), statement: document.statement.clone(), board: document.board.clone() }
}
fn mark(id: &str, revision: &str, verdict: Verdict, created_at: i64) -> MarkAttempt {
    MarkAttempt { id: Some(id.into()), task_source_id: TASK.into(), task_revision: revision.into(), verdict, source: AttemptSource::Manual, created_at: Some(created_at), code: "print(42)".into(), answer: "42".into() }
}

#[test]
fn own_document_commits_reopens_and_checks_optimistic_revision() {
    let sandbox = Sandbox::new(); let root = sandbox.at("store");
    let mut store = Store::open(&root).unwrap();
    assert!(store.snapshot().unwrap().tasks.is_empty());
    let initial = store.create_document(CreateDocument::default()).unwrap();
    assert_eq!(initial.title,"Своя работа"); assert!(initial.is_own_work); assert!(initial.code.is_empty()); assert!(initial.statement.is_empty()); assert_eq!(initial.board,json!({}));
    let mut input = save(&initial,"print('Привет')\n"); input.board = json!({"version":1,"items":[{"x":4,"label":"Черновик"}]});
    let saved = store.save_document(input).unwrap(); assert_eq!(saved.revision,2);
    let conflict = store.save_document(save(&initial,"stale")).unwrap_err();
    assert_eq!(conflict.code,"revision_conflict"); assert_eq!(conflict.conflict_revision,Some(2));
    assert!(store.verify_document_revision(&saved.id,2).is_ok()); assert!(store.verify_document_revision(&saved.id,1).is_err());
    drop(store);
    let mut reopened = Store::open(&root).unwrap();
    assert_eq!(reopened.document(&saved.id).unwrap(),saved);
    assert_eq!(reopened.snapshot().unwrap().preferences.selected_document_id,Some(saved.id));
    assert!(reopened.snapshot().unwrap().tasks.is_empty());
}

#[test]
fn invalid_document_does_not_mutate_saved_data() {
    let sandbox = Sandbox::new(); let mut store = Store::open(sandbox.at("store")).unwrap();
    let document = store.create_document(CreateDocument::default()).unwrap(); let before = store.snapshot().unwrap();
    let mut input = save(&document,"unchanged"); input.board = json!([]);
    assert_eq!(store.save_document(input).unwrap_err().code,"invalid_data");
    assert_eq!(store.snapshot().unwrap(),before);
    let mut input = save(&document,"unchanged"); input.type_number = Some(28);
    assert_eq!(store.save_document(input).unwrap_err().code,"invalid_data");
    assert_eq!(store.snapshot().unwrap(),before);
}

#[test]
fn attempt_history_derives_error_solved_and_last_verdict_without_erasure() {
    let sandbox = Sandbox::new(); let bundle = sandbox.archive("history.kontur",|c| { c.execute_batch("DELETE FROM attempts; DELETE FROM documents; UPDATE preferences SET value_json='null' WHERE key='selectedDocumentId';").unwrap(); });
    let root = sandbox.at("store"); let mut store = Store::open(&root).unwrap(); store.import_bundle(&bundle).unwrap();
    assert_eq!(store.snapshot().unwrap().progress[0].status,ProgressStatus::NotStarted);
    assert_eq!(store.mark_attempt(mark("manual-1",REV,Verdict::Error,1000)).unwrap().status,ProgressStatus::Error);
    assert_eq!(store.mark_attempt(mark("manual-2",REV,Verdict::Wrong,2000)).unwrap().status,ProgressStatus::Error);
    assert_eq!(store.mark_attempt(mark("manual-3",REV,Verdict::Unverified,3000)).unwrap().status,ProgressStatus::Error);
    assert_eq!(store.mark_attempt(mark("manual-4",REV,Verdict::Correct,4000)).unwrap().status,ProgressStatus::Solved);
    let last = store.mark_attempt(mark("manual-5",REV,Verdict::Error,5000)).unwrap();
    assert!(last.ever_solved); assert_eq!(last.status,ProgressStatus::Error); assert_eq!(last.last_verdict,Some(Verdict::Error)); assert_eq!(last.attempt_count,5);
    assert_eq!(store.mark_attempt(mark("manual-5",REV,Verdict::Error,5000)).unwrap(),last);
    let before = store.snapshot().unwrap();
    assert_eq!(store.mark_attempt(mark("manual-5",REV,Verdict::Wrong,5000)).unwrap_err().code,"record_conflict");
    assert_eq!(store.snapshot().unwrap(),before);
    assert_eq!(store.mark_attempt(mark("manual-6",REV,Verdict::Correct,6000)).unwrap().status,ProgressStatus::Solved);
    let wrong = store.mark_attempt(mark("manual-7",REV,Verdict::Wrong,7000)).unwrap(); assert_eq!(wrong.status,ProgressStatus::Error); assert!(wrong.ever_solved);
    assert_eq!(store.mark_attempt(mark("manual-8",REV,Verdict::Correct,8000)).unwrap().status,ProgressStatus::Solved);
    let last = store.mark_attempt(mark("manual-9",REV,Verdict::Unverified,9000)).unwrap(); assert_eq!(last.status,ProgressStatus::Solved); assert_eq!(last.last_verdict,Some(Verdict::Unverified)); assert_eq!(last.attempt_count,9);
    drop(store); let reopened = Store::open(root).unwrap();
    assert_eq!(reopened.snapshot().unwrap().progress[0],last); assert_eq!(reopened.snapshot().unwrap().attempts.len(),9);
}

#[test]
fn lost_ack_retry_reuses_native_timestamp_and_rejects_changed_payload() {
    let sandbox = Sandbox::new(); let fixture = sandbox.archive("native-retry.kontur",|c| { c.execute("DELETE FROM attempts",[]).unwrap(); });
    let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(fixture).unwrap();
    let mut request = mark("lost-ack-native-event",REV,Verdict::Correct,0); request.created_at = None;
    let first = store.mark_attempt(request.clone()).unwrap(); let saved = store.snapshot().unwrap();
    assert_eq!(saved.attempts.len(),1); assert!(saved.attempts[0].created_at > 0); assert_eq!(saved.attempts[0].source,AttemptSource::Manual);
    assert_eq!(store.mark_attempt(request.clone()).unwrap(),first); assert_eq!(store.snapshot().unwrap(),saved);
    request.code = "changed after lost acknowledgement".into(); assert_eq!(store.mark_attempt(request).unwrap_err().code,"record_conflict"); assert_eq!(store.snapshot().unwrap(),saved);
}

#[test]
fn duplicate_import_is_idempotent_and_persists_real_assets_and_bookmarks() {
    let sandbox = Sandbox::new(); let bundle = sandbox.archive("first.kontur",|_| {});
    let root = sandbox.at("store"); let mut store = Store::open(&root).unwrap();
    let preview = store.preview_import(&bundle).unwrap(); assert_eq!(preview.new_tasks,1); assert_eq!(preview.new_documents,1); assert_eq!(preview.asset_count,1);
    let imported = store.import_bundle_checked(&bundle,&preview.content_sha256).unwrap();
    assert_eq!(imported.imported_attempts,1); assert!(Path::new(&imported.backup_path).is_file());
    let before = store.snapshot().unwrap();
    assert_eq!(before.preferences.values["legacy.bookmarks"],json!([TASK]));
    assert_eq!(store.asset("synthetic-asset").unwrap().bytes,b"Synthetic attachment only.\n");
    let duplicate = store.import_bundle(&bundle).unwrap();
    assert_eq!(duplicate.imported_tasks+duplicate.imported_attempts+duplicate.imported_documents+duplicate.imported_assets,0);
    assert_eq!(duplicate.duplicate_records,4); assert!(duplicate.backup_path.is_empty());
    assert_eq!(store.snapshot().unwrap(),before);
    drop(store); assert_eq!(Store::open(root).unwrap().snapshot().unwrap(),before);
}

#[test]
fn changed_task_head_never_inherits_old_solved_and_old_import_never_rolls_back() {
    let sandbox = Sandbox::new(); let original = sandbox.archive("r1.kontur",|_| {});
    let revised = sandbox.archive("r2.kontur",|c| {
        c.execute("INSERT INTO tasks SELECT source_id,'synthetic-revision-2',type_number,title,'Новое условие',blocks_json,asset_ids_json FROM tasks WHERE revision=?1",[REV]).unwrap();
        c.execute("UPDATE task_heads SET revision='synthetic-revision-2'",[]).unwrap();
    });
    let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(&original).unwrap(); assert_eq!(store.snapshot().unwrap().progress[0].status,ProgressStatus::Solved);
    store.import_bundle(&revised).unwrap(); let current = store.snapshot().unwrap();
    assert_eq!(current.tasks[0].revision,"synthetic-revision-2"); assert_eq!(current.progress[0].status,ProgressStatus::NotStarted); assert!(!current.progress[0].ever_solved); assert_eq!(current.attempts.len(),1);
    store.import_bundle(&original).unwrap(); assert_eq!(store.snapshot().unwrap(),current);
    assert_eq!(store.mark_attempt(mark("new-error","synthetic-revision-2",Verdict::Error,100)).unwrap().status,ProgressStatus::Error);
    let output = sandbox.at("both-revisions.kontur"); store.export_bundle(&output).unwrap();
    let archive = Connection::open_with_flags(output,OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    assert_eq!(archive.query_row("SELECT count(*) FROM tasks",[],|r| r.get::<_,i64>(0)).unwrap(),2);
}

#[test]
fn unknown_historical_revision_keeps_verdict_and_id_but_never_affects_current_progress() {
    let sandbox = Sandbox::new(); let first = sandbox.archive("unknown.kontur",|c| { c.execute_batch("UPDATE attempts SET task_revision=NULL; DELETE FROM documents; UPDATE preferences SET value_json='null' WHERE key='selectedDocumentId';").unwrap(); });
    let newer = sandbox.archive("unknown-new-head.kontur",|c| {
        c.execute("UPDATE attempts SET task_revision=NULL",[]).unwrap();
        c.execute_batch("DELETE FROM documents; UPDATE preferences SET value_json='null' WHERE key='selectedDocumentId';").unwrap();
        c.execute("INSERT INTO tasks SELECT source_id,'synthetic-revision-2',type_number,title,'Изменённое условие',blocks_json,asset_ids_json FROM tasks",[]).unwrap();
        c.execute("UPDATE task_heads SET revision='synthetic-revision-2'",[]).unwrap();
    });
    let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(&first).unwrap();
    let snapshot = store.snapshot().unwrap(); assert_eq!(snapshot.attempts[0].task_revision,None); assert_eq!(snapshot.attempts[0].verdict,Verdict::Correct);
    assert_eq!(snapshot.progress[0].status,ProgressStatus::NotStarted); assert_eq!(snapshot.progress[0].attempt_count,0); assert_eq!(snapshot.progress[0].last_verdict,None);
    store.import_bundle(&newer).unwrap(); let newer = store.snapshot().unwrap(); assert_eq!(newer.attempts.len(),1); assert_eq!(newer.attempts[0].id,"synthetic-event"); assert_eq!(newer.progress[0].status,ProgressStatus::NotStarted);
    let output = sandbox.at("unknown-roundtrip.kontur"); store.export_bundle(&output).unwrap();
    let mut second = Store::open(sandbox.at("second")).unwrap(); second.import_bundle(output).unwrap(); assert_eq!(second.snapshot().unwrap(),newer);
}

#[test]
fn document_conflict_keeps_both_and_reimport_preserves_edits_to_retained_copy() {
    let sandbox = Sandbox::new(); let first = sandbox.archive("local.kontur",|_| {});
    let remote = sandbox.archive("remote.kontur",|c| { c.execute("UPDATE documents SET code='remote work',revision=2,updated_at=99",[]).unwrap(); });
    let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(first).unwrap();
    let local = store.document("synthetic-document").unwrap(); store.save_document(save(&local,"local work")).unwrap();
    assert_eq!(store.preview_import(&remote).unwrap().document_conflicts,1);
    let result = store.import_bundle(&remote).unwrap(); assert_eq!(result.document_conflicts,1); assert_eq!(result.imported_documents,1);
    let documents = store.snapshot().unwrap().documents; assert_eq!(documents.len(),2);
    let retained = documents.iter().find(|d| d.id.starts_with("doc-import-")).unwrap(); assert_eq!(retained.code,"remote work");
    store.save_document(save(retained,"edited retained work")).unwrap(); let before = store.snapshot().unwrap();
    let repeated = store.import_bundle(&remote).unwrap(); assert_eq!(repeated.imported_documents,0); assert_eq!(repeated.document_conflicts,0); assert_eq!(store.snapshot().unwrap(),before);
    assert_eq!(store.document("synthetic-document").unwrap().code,"local work");
}

#[test]
fn transaction_failure_rolls_back_prior_inserts_and_backup_is_readable() {
    let sandbox = Sandbox::new(); let bundle = sandbox.archive("rollback.kontur",|_| {}); let root = sandbox.at("store");
    let mut store = Store::open(&root).unwrap(); store.create_document(CreateDocument::default()).unwrap(); let before = store.snapshot().unwrap();
    let database = Connection::open(root.join("kontur.sqlite3")).unwrap();
    database.execute_batch("CREATE TRIGGER synthetic_abort BEFORE INSERT ON attempts BEGIN SELECT RAISE(ABORT,'synthetic private diagnostic'); END;").unwrap();
    assert!(store.preview_import(&bundle).is_ok()); let error = store.import_bundle(&bundle).unwrap_err();
    assert_eq!(error.code,"storage_error"); assert!(!error.message.contains("synthetic private diagnostic")); assert_eq!(store.snapshot().unwrap(),before);
    let backups: Vec<_> = fs::read_dir(root.join("backups")).unwrap().map(|entry| entry.unwrap().path()).collect(); assert_eq!(backups.len(),1);
    let backup = Connection::open_with_flags(&backups[0],OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    assert_eq!(backup.query_row("SELECT count(*) FROM documents",[],|r| r.get::<_,i64>(0)).unwrap(),1);
    assert_eq!(backup.query_row("SELECT count(*) FROM tasks",[],|r| r.get::<_,i64>(0)).unwrap(),0);
    database.execute_batch("DROP TRIGGER synthetic_abort").unwrap(); drop(backup); drop(database); drop(store);
    assert_eq!(Store::open(root).unwrap().snapshot().unwrap(),before);
}

#[test]
fn preview_digest_binds_confirmation_to_the_validated_file_bytes() {
    let sandbox = Sandbox::new(); let bundle = sandbox.archive("changed.kontur",|_| {}); let root = sandbox.at("store");
    let mut store = Store::open(&root).unwrap(); let before = store.snapshot().unwrap(); let preview = store.preview_import(&bundle).unwrap();
    let connection = Connection::open(&bundle).unwrap(); connection.execute("UPDATE documents SET code='changed after preview'",[]).unwrap(); connection.close().unwrap();
    assert_eq!(store.import_bundle_checked(&bundle,&preview.content_sha256).unwrap_err().code,"import_changed"); assert_eq!(store.snapshot().unwrap(),before); assert!(!root.join("backups").exists());
}

#[test]
fn invalid_schema_runtime_types_references_checksums_and_limits_leave_database_unchanged() {
    let sandbox = Sandbox::new(); let root = sandbox.at("store"); let mut store = Store::open(&root).unwrap(); store.create_document(CreateDocument::default()).unwrap(); let before = store.snapshot().unwrap();
    let mutations: Vec<(&str,Box<dyn Fn(&Connection)>)> = vec![
        ("view",Box::new(|c| { c.execute_batch("CREATE VIEW synthetic_view AS SELECT 1 AS x").unwrap(); })),
        ("trigger",Box::new(|c| { c.execute_batch("CREATE TRIGGER synthetic_trigger AFTER INSERT ON documents BEGIN SELECT 1; END").unwrap(); })),
        ("index",Box::new(|c| { c.execute_batch("CREATE INDEX synthetic_index ON tasks(title)").unwrap(); })),
        ("extra-table",Box::new(|c| { c.execute_batch("CREATE TABLE unexpected(x TEXT)").unwrap(); })),
        ("extra-column",Box::new(|c| { c.execute_batch("ALTER TABLE tasks ADD COLUMN unexpected TEXT").unwrap(); })),
        ("added-check",Box::new(|c| { c.execute_batch("PRAGMA writable_schema=ON; UPDATE sqlite_schema SET sql=replace(sql,'code TEXT NOT NULL','code TEXT NOT NULL CHECK(length(randomblob(16))=16)') WHERE name='documents'; PRAGMA writable_schema=OFF;").unwrap(); })),
        ("added-default",Box::new(|c| { c.execute_batch("PRAGMA writable_schema=ON; UPDATE sqlite_schema SET sql=replace(sql,'title TEXT NOT NULL','title TEXT NOT NULL DEFAULT 1') WHERE name='documents'; PRAGMA writable_schema=OFF;").unwrap(); })),
        ("blob-text",Box::new(|c| { c.execute_batch("UPDATE documents SET code=x'00'").unwrap(); })),
        ("real-integer",Box::new(|c| { c.execute_batch("UPDATE tasks SET type_number=2.5").unwrap(); })),
        ("bad-utf8",Box::new(|c| { c.execute_batch("UPDATE tasks SET title=CAST(x'80' AS TEXT)").unwrap(); })),
        ("missing-asset",Box::new(|c| { c.execute_batch("UPDATE tasks SET asset_ids_json='[\"missing\"]'").unwrap(); })),
        ("duplicate-asset",Box::new(|c| { c.execute_batch("UPDATE tasks SET asset_ids_json='[\"synthetic-asset\",\"synthetic-asset\"]'").unwrap(); })),
        ("wrong-hash",Box::new(|c| { c.execute_batch("UPDATE assets SET bytes=x'00'").unwrap(); })),
        ("negative-asset-bytes",Box::new(|c| { c.execute_batch("UPDATE assets SET bytes=-1").unwrap(); })),
        ("asset-path",Box::new(|c| { c.execute_batch("UPDATE assets SET name='../unsafe.txt'").unwrap(); })),
        ("missing-task",Box::new(|c| { c.execute_batch("UPDATE attempts SET task_source_id='missing'").unwrap(); })),
        ("wrong-revision",Box::new(|c| { c.execute_batch("UPDATE attempts SET task_revision='missing'").unwrap(); })),
        ("half-document-ref",Box::new(|c| { c.execute_batch("UPDATE documents SET task_revision=NULL").unwrap(); })),
        ("bad-head",Box::new(|c| { c.execute_batch("UPDATE task_heads SET revision='missing'").unwrap(); })),
        ("invalid-board",Box::new(|c| { c.execute_batch("UPDATE documents SET board_json='[]'").unwrap(); })),
        ("invalid-selection",Box::new(|c| { c.execute_batch("UPDATE preferences SET value_json='\"missing\"' WHERE key='selectedDocumentId'").unwrap(); })),
        ("oversize",Box::new(|c| { c.execute("UPDATE documents SET code=?1",["x".repeat(MAX_BODY_BYTES+1)]).unwrap(); })),
        ("deep-json",Box::new(|c| { let deep = format!("{{\"x\":{}{}}}","[".repeat(150),"]".repeat(150)); c.execute("UPDATE documents SET board_json=?1",[deep]).unwrap(); })),
    ];
    for (name,mutate) in mutations {
        let path = sandbox.archive(&format!("invalid-{name}.kontur"),|c| mutate(c));
        assert!(store.preview_import(&path).is_err(),"preview accepted {name}"); assert!(store.import_bundle(&path).is_err(),"import accepted {name}"); assert_eq!(store.snapshot().unwrap(),before,"mutation after {name}");
    }
    assert!(!root.join("backups").exists());
}

#[test]
fn unsupported_or_corrupt_containers_are_not_treated_as_sql_scripts() {
    let sandbox = Sandbox::new(); let root = sandbox.at("store"); let mut store = Store::open(&root).unwrap(); let before = store.snapshot().unwrap();
    let unsupported = sandbox.archive("unsupported.kontur",|c| { c.pragma_update(None,"user_version",2).unwrap(); });
    assert_eq!(store.preview_import(unsupported).unwrap_err().code,"unsupported_format");
    let sql = sandbox.at("script.kontur"); fs::write(&sql,b"DROP TABLE documents; -- not SQLite").unwrap(); assert!(store.import_bundle(sql).is_err());
    let corrupt = sandbox.at("corrupt.kontur"); let mut bytes = vec![0u8;4096]; bytes[..16].copy_from_slice(b"SQLite format 3\0"); fs::write(&corrupt,bytes).unwrap(); assert!(store.import_bundle(corrupt).is_err());
    assert_eq!(store.snapshot().unwrap(),before); assert!(!root.join("backups").exists());
}

#[test]
fn full_integrity_check_rejects_primary_key_mismatch_that_quick_check_misses() {
    let sandbox = Sandbox::new(); let path = sandbox.archive("corrupt-index.kontur",|_| {});
    let connection = Connection::open_with_flags(&path,OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let page: i64 = connection.query_row("SELECT rootpage FROM sqlite_schema WHERE name='sqlite_autoindex_preferences_1'",[],|r| r.get::<_,i64>(0)).unwrap();
    let page_size: i64 = connection.pragma_query_value(None,"page_size",|r| r.get::<_,i64>(0)).unwrap(); connection.close().unwrap();
    let page = usize::try_from(page).expect("Synthetic index page must fit a nonnegative memory offset");
    let page_size = usize::try_from(page_size).expect("Synthetic page size must fit a nonnegative memory length");
    let mut bytes = fs::read(&path).unwrap(); let offset = (page-1)*page_size; assert_eq!(bytes[offset],0x0a); assert_eq!(page_size,4096);
    // Change one equal-size key only in this synthetic index leaf. Cell count,
    // record types and sorted order remain valid (legacy < t... < ui.theme).
    // The unchanged table key no longer matches its PK index entry.
    let key = b"selectedDocumentId";
    let key_offset = bytes[offset..offset+page_size].windows(key.len()).position(|window| window==key).unwrap();
    bytes[offset+key_offset] = b't'; fs::write(&path,bytes).unwrap();
    let connection = Connection::open_with_flags(&path,OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    assert_eq!(connection.query_row("PRAGMA quick_check(1)",[],|r| r.get::<_,String>(0)).unwrap(),"ok");
    assert_ne!(connection.query_row("PRAGMA integrity_check(1)",[],|r| r.get::<_,String>(0)).unwrap(),"ok"); connection.close().unwrap();
    let root = sandbox.at("store"); let mut store = Store::open(&root).unwrap(); let before = store.snapshot().unwrap();
    assert_eq!(store.preview_import(&path).unwrap_err().code,"invalid_data"); assert_eq!(store.import_bundle(path).unwrap_err().code,"invalid_data"); assert_eq!(store.snapshot().unwrap(),before); assert!(!root.join("backups").exists());
}

#[test]
fn immutable_source_revision_event_and_asset_ids_reject_changed_bodies() {
    let sandbox = Sandbox::new(); let original = sandbox.archive("original.kontur",|_| {}); let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(original).unwrap(); let before = store.snapshot().unwrap();
    let changed_task = sandbox.archive("changed-task.kontur",|c| { c.execute_batch("UPDATE tasks SET statement='Different content under the same revision'").unwrap(); });
    let changed_event = sandbox.archive("changed-event.kontur",|c| { c.execute_batch("UPDATE attempts SET verdict='wrong'").unwrap(); });
    let changed_asset = sandbox.archive("changed-asset.kontur",|c| { let bytes = b"different valid bytes"; c.execute("UPDATE assets SET bytes=?1,sha256=?2",params![bytes.as_slice(),sha256(bytes).unwrap()]).unwrap(); });
    for path in [changed_task,changed_event,changed_asset] { assert_eq!(store.preview_import(&path).unwrap_err().code,"record_conflict"); assert_eq!(store.import_bundle(path).unwrap_err().code,"record_conflict"); assert_eq!(store.snapshot().unwrap(),before); }
}

#[test]
fn native_export_is_complete_reimportable_read_only_and_does_not_clobber() {
    let sandbox = Sandbox::new(); let input = sandbox.archive("input.kontur",|_| {}); let mut source = Store::open(sandbox.at("source")).unwrap(); source.import_bundle(input).unwrap();
    let output = sandbox.at("output.kontur"); let result = source.export_bundle(&output).unwrap(); assert_eq!(result.task_count,1); assert_eq!(result.asset_count,1); assert_eq!(sha256(&fs::read(&output).unwrap()).unwrap(),result.content_sha256);
    let bytes = fs::read(&output).unwrap(); assert_eq!(source.export_bundle(&output).unwrap_err().code,"destination_exists"); assert_eq!(fs::read(&output).unwrap(),bytes);
    let archive = Connection::open_with_flags(&output,OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    assert!(archive.execute("DELETE FROM tasks",[]).is_err());
    assert_eq!(archive.query_row("SELECT count(*) FROM sqlite_schema WHERE name='import_receipts'",[],|r| r.get::<_,i64>(0)).unwrap(),0); drop(archive);
    let mut destination = Store::open(sandbox.at("destination")).unwrap(); destination.import_bundle(&output).unwrap(); assert_eq!(destination.snapshot().unwrap(),source.snapshot().unwrap()); assert_eq!(destination.asset("synthetic-asset").unwrap(),source.asset("synthetic-asset").unwrap());
}

#[test]
#[cfg(windows)]
fn native_export_and_reimport_support_long_cyrillic_windows_destination() {
    use std::os::windows::ffi::OsStrExt;
    let sandbox = Sandbox::new(); let input = sandbox.archive("long-path-input.kontur",|_| {});
    let mut source = Store::open(sandbox.at("source")).unwrap(); source.import_bundle(input).unwrap();
    // Two bounded components inside this test's disposable, verified sandbox.
    let directory = sandbox.at(&"д".repeat(110)).join("я".repeat(110)); fs::create_dir_all(&directory).unwrap();
    let destination = directory.join("Перенос_данных.kontur"); let units = destination.as_os_str().encode_wide().count();
    assert!(units > 300 && units < 512,"Synthetic path must exercise the Windows long-path boundary: {units}"); assert!(!destination.exists());
    let result = source.export_bundle(&destination).unwrap(); assert!(destination.is_file());
    assert_eq!(sha256(&fs::read(&destination).unwrap()).unwrap(),result.content_sha256);
    assert_eq!(source.export_bundle(&destination).unwrap_err().code,"destination_exists");
    let mut target = Store::open(sandbox.at("reimported")).unwrap(); target.import_bundle(&destination).unwrap();
    assert_eq!(target.snapshot().unwrap(),source.snapshot().unwrap()); assert_eq!(target.asset("synthetic-asset").unwrap(),source.asset("synthetic-asset").unwrap());
}

#[test]
fn independent_python_exporter_fixture_imports_and_roundtrips_with_exact_history() {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("../qa/exporter_fixture.kontur");
    assert!(fixture.is_file(),"The independently produced synthetic Python fixture must be present.");
    let sandbox = Sandbox::new(); let mut store = Store::open(sandbox.at("store")).unwrap();
    let preview = store.preview_import(&fixture).unwrap(); assert_eq!(preview.task_count,2); assert_eq!(preview.attempt_count,4); assert_eq!(preview.asset_count,1);
    store.import_bundle_checked(&fixture,&preview.content_sha256).unwrap(); let snapshot = store.snapshot().unwrap();
    assert_eq!(snapshot.tasks.len(),1); assert_eq!(snapshot.tasks[0].source_id,"synthetic:task:1"); assert_eq!(snapshot.tasks[0].revision,"revision-current");
    let progress = &snapshot.progress[0]; assert_eq!(progress.status,ProgressStatus::Error); assert!(progress.ever_solved); assert_eq!(progress.attempt_count,2); assert_eq!(progress.last_attempt_id.as_deref(),Some("event-2"));
    assert_eq!(snapshot.attempts.len(),4); assert_eq!(snapshot.attempts.iter().find(|a| a.id=="event-unknown").unwrap().task_revision,None);
    assert_eq!(store.asset("asset-hello").unwrap().bytes,b"hello"); assert_eq!(snapshot.documents[0].board,json!({"strokes":[[0,1]],"version":1})); assert_eq!(snapshot.preferences.values["kontur.legacy12.bookmarks"],json!(["synthetic:task:1"]));
    let exported = sandbox.at("python-native-roundtrip.kontur"); let result = store.export_bundle(&exported).unwrap(); assert_eq!(result.task_count,2);
    let mut target = Store::open(sandbox.at("target")).unwrap(); target.import_bundle(exported).unwrap(); assert_eq!(target.snapshot().unwrap(),snapshot);
}

#[test]
fn linked_document_starts_only_its_exact_revision_and_unverified_preserves_evaluation() {
    let sandbox = Sandbox::new(); let fixture = sandbox.archive("started.kontur",|c| { c.execute_batch("DELETE FROM attempts; DELETE FROM documents; UPDATE preferences SET value_json='null' WHERE key='selectedDocumentId';").unwrap(); });
    let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(fixture).unwrap(); assert_eq!(store.snapshot().unwrap().progress[0].status,ProgressStatus::NotStarted);
    store.create_document(CreateDocument { task_source_id:Some(TASK.into()),task_revision:Some(REV.into()),..Default::default() }).unwrap(); assert_eq!(store.snapshot().unwrap().progress[0].status,ProgressStatus::InProgress);
    assert_eq!(store.mark_attempt(mark("only-unverified",REV,Verdict::Unverified,100)).unwrap().status,ProgressStatus::InProgress);
    assert_eq!(store.mark_attempt(mark("a-correct",REV,Verdict::Correct,200)).unwrap().status,ProgressStatus::Solved);
    let last = store.mark_attempt(mark("z-wrong",REV,Verdict::Wrong,200)).unwrap(); assert_eq!(last.status,ProgressStatus::Error); assert!(last.ever_solved); assert_eq!(last.last_attempt_id.as_deref(),Some("z-wrong"));
    let last = store.mark_attempt(mark("unverified-later",REV,Verdict::Unverified,300)).unwrap(); assert_eq!(last.status,ProgressStatus::Error); assert_eq!(last.last_verdict,Some(Verdict::Unverified));
}

#[test]
fn reserved_transfer_warnings_union_keeps_new_codes_and_other_local_preferences() {
    let sandbox = Sandbox::new();
    let empty = sandbox.archive("warnings-empty.kontur",|c| { c.execute("INSERT INTO preferences VALUES (?1,'[]')",[WARNINGS]).unwrap(); });
    let newer = sandbox.archive("warnings-new.kontur",|c| {
        c.execute("INSERT INTO preferences VALUES (?1,?2)",params![WARNINGS,serde_json::to_string(&json!(["new_warning","old_warning","new_warning"])).unwrap()]).unwrap();
        c.execute_batch("UPDATE preferences SET value_json='\"light\"' WHERE key='ui.theme'; INSERT INTO preferences VALUES ('ui.fontSize','14');").unwrap();
    });
    let later = sandbox.archive("warnings-later.kontur",|c| { c.execute("INSERT INTO preferences VALUES (?1,?2)",params![WARNINGS,serde_json::to_string(&json!(["old_warning","later_warning"])).unwrap()]).unwrap(); });
    let mut store = Store::open(sandbox.at("store")).unwrap(); store.import_bundle(&empty).unwrap();
    assert_eq!(store.snapshot().unwrap().preferences.values[WARNINGS],json!([]));
    let result = store.import_bundle(&newer).unwrap();
    assert_eq!(result.imported_tasks+result.imported_attempts+result.imported_documents+result.imported_assets,0); assert!(!result.backup_path.is_empty());
    let snapshot = store.snapshot().unwrap(); assert_eq!(snapshot.preferences.values[WARNINGS],json!(["new_warning","old_warning"]));
    assert_eq!(snapshot.preferences.values["ui.theme"],json!("dark")); assert_eq!(snapshot.preferences.values["ui.fontSize"],json!(14)); assert_eq!(snapshot.preferences.selected_document_id.as_deref(),Some("synthetic-document"));
    store.import_bundle(&later).unwrap(); let before = store.snapshot().unwrap(); assert_eq!(before.preferences.values[WARNINGS],json!(["later_warning","new_warning","old_warning"]));
    assert!(store.import_bundle(&newer).unwrap().backup_path.is_empty()); assert!(store.import_bundle(&empty).unwrap().backup_path.is_empty()); assert_eq!(store.snapshot().unwrap(),before);
}

#[test]
fn malformed_reserved_warning_types_reject_input_and_local_state_without_mutation() {
    let sandbox = Sandbox::new(); let root = sandbox.at("store");
    let valid = sandbox.archive("warnings-valid.kontur",|c| { c.execute("INSERT INTO preferences VALUES (?1,'[\"known_warning\"]')",[WARNINGS]).unwrap(); });
    let mut store = Store::open(&root).unwrap(); store.import_bundle(&valid).unwrap(); let before = store.snapshot().unwrap();
    let backup_count = fs::read_dir(root.join("backups")).unwrap().count();
    let invalid = vec![(json!(null),"invalid_data"),(json!("wrong"),"invalid_data"),(json!({"code":"wrong"}),"invalid_data"),(json!([1]),"invalid_data"),(json!([""]),"invalid_data"),(json!(["has\ncontrol"]),"invalid_data"),(json!(["x".repeat(257)]),"limit_exceeded")];
    for (index,(value,expected_code)) in invalid.into_iter().enumerate() {
        let path = sandbox.archive(&format!("warnings-malformed-{index}.kontur"),|c| { c.execute("INSERT INTO preferences VALUES (?1,?2)",params![WARNINGS,serde_json::to_string(&value).unwrap()]).unwrap(); });
        assert_eq!(store.preview_import(&path).unwrap_err().code,expected_code); assert_eq!(store.import_bundle(&path).unwrap_err().code,expected_code); assert_eq!(store.snapshot().unwrap(),before);
    }
    // A prior malformed local value must also be rejected, never replaced/coerced.
    let database = Connection::open(root.join("kontur.sqlite3")).unwrap(); database.execute("UPDATE preferences SET value_json='\"wrong\"' WHERE key=?1",[WARNINGS]).unwrap(); database.close().unwrap();
    let local_before = store.snapshot().unwrap(); assert_eq!(store.preview_import(&valid).unwrap_err().code,"invalid_data"); assert_eq!(store.import_bundle(&valid).unwrap_err().code,"invalid_data"); assert_eq!(store.snapshot().unwrap(),local_before); assert_eq!(fs::read_dir(root.join("backups")).unwrap().count(),backup_count);
}

#[test]
fn warning_union_overflow_is_rejected_before_backup_or_mutation() {
    let sandbox = Sandbox::new(); let root = sandbox.at("store");
    let codes = |prefix: &str| (0..150).map(|index| format!("{prefix}_{index:03}_{}","x".repeat(240))).collect::<Vec<_>>();
    let left = codes("a"); let right = codes("b");
    assert!(serde_json::to_string(&left).unwrap().len() < 64*1024); assert!(serde_json::to_string(&right).unwrap().len() < 64*1024); assert!(serde_json::to_string(&left.iter().chain(right.iter()).collect::<Vec<_>>()).unwrap().len() > 64*1024);
    let first = sandbox.archive("warnings-bounded-a.kontur",|c| { c.execute("INSERT INTO preferences VALUES (?1,?2)",params![WARNINGS,serde_json::to_string(&left).unwrap()]).unwrap(); });
    let second = sandbox.archive("warnings-bounded-b.kontur",|c| { c.execute("INSERT INTO preferences VALUES (?1,?2)",params![WARNINGS,serde_json::to_string(&right).unwrap()]).unwrap(); });
    let mut store = Store::open(&root).unwrap(); store.import_bundle(&first).unwrap(); let before = store.snapshot().unwrap(); let backup_count = fs::read_dir(root.join("backups")).unwrap().count();
    assert_eq!(store.preview_import(&second).unwrap_err().code,"limit_exceeded"); assert_eq!(store.import_bundle(&second).unwrap_err().code,"limit_exceeded"); assert_eq!(store.snapshot().unwrap(),before); assert_eq!(fs::read_dir(root.join("backups")).unwrap().count(),backup_count);
}

#[test]
fn timestamp_date_boundary_is_valid_and_larger_imports_leave_data_unchanged() {
    let sandbox = Sandbox::new(); let root = sandbox.at("store");
    let date_max: i64 = 8_640_000_000_000_000; let safe_integer_max: i64 = 9_007_199_254_740_991;
    let boundary = sandbox.archive("timestamp-boundary.kontur",|c| {
        c.execute("UPDATE attempts SET created_at=?1",[date_max]).unwrap();
        c.execute("UPDATE documents SET updated_at=?1,revision=?2",params![date_max,safe_integer_max]).unwrap();
        c.execute("INSERT INTO metadata VALUES ('createdAt',?1)",[date_max.to_string()]).unwrap();
    });
    let mut store = Store::open(&root).unwrap(); store.create_document(CreateDocument::default()).unwrap();
    assert!(store.preview_import(&boundary).is_ok()); store.import_bundle(&boundary).unwrap(); let before = store.snapshot().unwrap();
    assert_eq!(before.attempts[0].created_at,date_max);
    let document = before.documents.iter().find(|d| d.id=="synthetic-document").unwrap(); assert_eq!(document.updated_at,date_max); assert_eq!(document.revision,safe_integer_max);
    let backup_count = fs::read_dir(root.join("backups")).unwrap().count();
    for (field,value) in [("document",date_max+1),("attempt",date_max+1),("metadata",date_max+1),("document",safe_integer_max),("attempt",safe_integer_max),("metadata",safe_integer_max)] {
        let invalid = sandbox.archive(&format!("timestamp-{field}-{value}.kontur"),|c| {
            match field {
                "document" => { c.execute("UPDATE documents SET updated_at=?1",[value]).unwrap(); },
                "attempt" => { c.execute("UPDATE attempts SET created_at=?1",[value]).unwrap(); },
                "metadata" => { c.execute("INSERT INTO metadata VALUES ('createdAt',?1)",[value.to_string()]).unwrap(); },
                _ => unreachable!("Only constant synthetic timestamp fields are used"),
            }
        });
        assert_eq!(store.preview_import(&invalid).unwrap_err().code,"invalid_data"); assert_eq!(store.import_bundle(&invalid).unwrap_err().code,"invalid_data"); assert_eq!(store.snapshot().unwrap(),before);
    }
    assert_eq!(fs::read_dir(root.join("backups")).unwrap().count(),backup_count);
}

#[test]
fn wire_is_camel_case_and_rejects_unknown_mutation_fields() {
    let encoded = serde_json::to_value(AppError::conflict(4)).unwrap(); assert_eq!(encoded,json!({"code":"revision_conflict","message":"Документ уже изменён. Загрузите сохранённую версию.","conflictRevision":4}));
    assert!(serde_json::to_value(AppError::new("safe","message")).unwrap().get("conflictRevision").is_none());
    assert!(serde_json::from_value::<CreateDocument>(json!({"sql":"SELECT 1"})).is_err());
    let attempt = Attempt { id:"synthetic".into(),task_source_id:TASK.into(),task_revision:None,verdict:Verdict::Correct,source:AttemptSource::Imported,created_at:1,code:String::new(),answer:String::new() };
    let value = serde_json::to_value(attempt).unwrap(); assert_eq!(value["taskRevision"],json!(null)); assert!(value.get("task_revision").is_none());
}
