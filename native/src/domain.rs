//! The public native wire contract. All JSON field names are camelCase.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub type AppResult<T> = Result<T, AppError>;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AppError {
    pub code: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conflict_revision: Option<i64>,
}

impl AppError {
    pub fn new(code: &str, message: &str) -> Self {
        Self { code: code.into(), message: message.into(), conflict_revision: None }
    }

    pub fn conflict(revision: i64) -> Self {
        Self { code: "revision_conflict".into(), message: "Документ уже изменён. Загрузите сохранённую версию.".into(), conflict_revision: Some(revision) }
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for AppError {}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Task {
    pub source_id: String,
    pub revision: String,
    pub type_number: u8,
    pub title: String,
    pub statement: String,
    pub blocks: Option<Value>,
    pub asset_ids: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Verdict { Correct, Wrong, Unverified, Error }

impl Verdict {
    pub(crate) fn as_str(self) -> &'static str {
        match self { Self::Correct => "correct", Self::Wrong => "wrong", Self::Unverified => "unverified", Self::Error => "error" }
    }
    pub(crate) fn parse(value: &str) -> AppResult<Self> {
        match value { "correct" => Ok(Self::Correct), "wrong" => Ok(Self::Wrong), "unverified" => Ok(Self::Unverified), "error" => Ok(Self::Error), _ => Err(invalid_data()) }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum AttemptSource { Imported, #[default] Manual }

impl AttemptSource {
    pub(crate) fn as_str(self) -> &'static str { match self { Self::Imported => "imported", Self::Manual => "manual" } }
    pub(crate) fn parse(value: &str) -> AppResult<Self> {
        match value { "imported" => Ok(Self::Imported), "manual" => Ok(Self::Manual), _ => Err(invalid_data()) }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Attempt {
    pub id: String,
    pub task_source_id: String,
    /// None preserves imported history whose actual task revision is unknown.
    pub task_revision: Option<String>,
    pub verdict: Verdict,
    pub source: AttemptSource,
    pub created_at: i64,
    pub code: String,
    pub answer: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Document {
    pub id: String,
    pub task_source_id: Option<String>,
    pub task_revision: Option<String>,
    pub title: String,
    pub type_number: Option<u8>,
    pub code: String,
    pub statement: String,
    /// An object owned and versioned by the board UI. New documents use {}.
    pub board: Value,
    pub revision: i64,
    pub updated_at: i64,
    /// Derived from the absence of an imported task reference.
    pub is_own_work: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProgressStatus { NotStarted, InProgress, Error, Solved }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ProgressSummary {
    pub task_source_id: String,
    pub task_revision: String,
    pub status: ProgressStatus,
    pub ever_solved: bool,
    pub last_verdict: Option<Verdict>,
    pub last_attempt_id: Option<String>,
    pub attempt_count: u64,
    pub updated_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AssetMetadata {
    pub id: String,
    pub name: String,
    pub mime: String,
    pub size_bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct AssetData {
    pub metadata: AssetMetadata,
    /// Tauri serializes bytes as an array of integers 0..255.
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
#[serde(rename_all = "camelCase")]
pub struct Preferences {
    pub selected_document_id: Option<String>,
    pub values: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    /// Current task revision per stable source ID; archives retain every revision.
    pub tasks: Vec<Task>,
    pub documents: Vec<Document>,
    pub attempts: Vec<Attempt>,
    /// Current task revisions only. Old attempts never solve a new revision.
    pub progress: Vec<ProgressSummary>,
    pub assets: Vec<AssetMetadata>,
    pub preferences: Preferences,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CreateDocument {
    pub task_source_id: Option<String>,
    pub task_revision: Option<String>,
    pub title: Option<String>,
    pub type_number: Option<u8>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SaveDocument {
    pub document_id: String,
    pub expected_revision: i64,
    pub title: String,
    pub type_number: Option<u8>,
    pub code: String,
    pub statement: String,
    pub board: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct MarkAttempt {
    pub id: Option<String>,
    pub task_source_id: String,
    pub task_revision: String,
    pub verdict: Verdict,
    #[serde(default)]
    pub source: AttemptSource,
    pub created_at: Option<i64>,
    #[serde(default)]
    pub code: String,
    #[serde(default)]
    pub answer: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ImportPreview {
    pub format_version: u32,
    pub content_sha256: String,
    pub task_count: u64,
    pub attempt_count: u64,
    pub document_count: u64,
    pub asset_count: u64,
    pub new_tasks: u64,
    pub new_attempts: u64,
    pub new_documents: u64,
    pub new_assets: u64,
    pub document_conflicts: u64,
    pub duplicate_records: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ImportResult {
    pub content_sha256: String,
    pub imported_tasks: u64,
    pub imported_attempts: u64,
    pub imported_documents: u64,
    pub imported_assets: u64,
    pub document_conflicts: u64,
    pub duplicate_records: u64,
    pub backup_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ExportResult {
    pub path: String,
    pub content_sha256: String,
    pub task_count: u64,
    pub attempt_count: u64,
    pub document_count: u64,
    pub asset_count: u64,
}

pub(crate) fn invalid_data() -> AppError { AppError::new("invalid_data", "Данные не соответствуют формату Контур.") }
pub(crate) fn storage_error() -> AppError { AppError::new("storage_error", "Не удалось надёжно сохранить или прочитать локальные данные.") }
pub(crate) fn io_error() -> AppError { AppError::new("file_error", "Не удалось прочитать или записать выбранный файл.") }
