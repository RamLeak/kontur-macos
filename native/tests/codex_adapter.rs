//! Isolated adapter tests. This fake platform NEVER creates an OS process.
//! Real Windows Job/pipe behavior belongs to native platform acceptance.
#![allow(dead_code)]
mod platform {
    use std::{ffi::OsString, path::PathBuf};
    pub struct ProcessSpec {
        pub program: PathBuf, pub args: Vec<OsString>, pub cwd: PathBuf,
        pub env: Vec<(OsString, OsString)>, pub stdin: Vec<u8>,
        pub stdout_limit: usize, pub stderr_limit: usize,
    }
    pub struct ProcessSnapshot {
        pub stdout: Vec<u8>, pub stderr_bytes: usize,
        pub exit_code: Option<u32>, pub output_overflow: bool,
    }
    pub struct ManagedProcess;
    impl ManagedProcess {
        pub fn spawn(_: ProcessSpec) -> Result<Self, ()> { Err(()) }
        pub fn poll(&mut self) -> Result<ProcessSnapshot, ()> { Err(()) }
        pub fn cancel(&mut self) -> Result<(), ()> { Err(()) }
    }
}
#[path = "../src/codex.rs"]
mod codex;
use codex::*;
use platform::{ProcessSnapshot, ProcessSpec};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const VALID_EVENTS: &str = concat!(
    "{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}\n",
    "{\"type\":\"turn.started\"}\n",
    "{\"type\":\"item.completed\",\"item\":{\"id\":\"a\",\"type\":\"agent_message\",\"text\":\"Ответ ✓\"}}\n",
    "{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":1}}\n",
);

fn metadata_warning(id: &str) -> String {
    serde_json::json!({"type":"item.completed","item":{
        "id":id,"type":"error","message":"Model metadata for `synthetic-test-only` not found. Defaulting to fallback metadata; this can degrade performance and cause issues."
    }}).to_string() + "\n"
}

#[test]
fn pinned_cli_catalog_warning_before_turn_does_not_discard_the_answer() {
    let events = VALID_EVENTS.replacen("{\"type\":\"turn.started\"}", &(metadata_warning("warning_0") + "{\"type\":\"turn.started\"}"), 1);
    assert_eq!(decode(&events).unwrap(), "Ответ ✓");
    assert_eq!(decode(&events.replace("{\"type\":\"turn.started\"}\n", "")).unwrap_err().code, "codex_protocol");
}

#[test]
fn catalog_warning_never_accepts_tools_duplicate_ids_or_unrecognized_errors() {
    let before_turn = VALID_EVENTS.replacen("{\"type\":\"turn.started\"}", &(metadata_warning("warning_0") + "{\"type\":\"turn.started\"}"), 1);
    let duplicate = before_turn.replacen(&metadata_warning("warning_0"), &(metadata_warning("warning_0").repeat(2)), 1);
    assert_eq!(decode(&duplicate).unwrap_err().code, "codex_protocol");
    let reused = before_turn.replace("warning_0", "a");
    assert_eq!(decode(&reused).unwrap_err().code, "codex_protocol");
    let unknown = before_turn.replace("Model metadata for `synthetic-test-only` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.", "Unrecognized diagnostic");
    assert_eq!(decode(&unknown).unwrap_err().code, "codex_protocol");
    let injected = before_turn.replace("\"id\":\"warning_0\"", "\"id\":\"warning_0\",\"nested\":{\"type\":\"command_execution\",\"command\":\"forbidden\"}");
    assert_eq!(decode(&injected).unwrap_err().code, "codex_tool_event");
}

#[test]
fn catalog_diagnostics_obey_item_and_terminal_bounds() {
    let warnings = (0..129).map(|id| metadata_warning(&format!("warning_{id}"))).collect::<String>();
    let events = VALID_EVENTS.replacen("{\"type\":\"turn.started\"}", &(warnings + "{\"type\":\"turn.started\"}"), 1);
    assert_eq!(decode(&events).unwrap_err().code, "codex_output_limit");
    let after_terminal = VALID_EVENTS.to_owned() + &metadata_warning("late_warning");
    assert_eq!(decode(&after_terminal).unwrap_err().code, "codex_protocol");
    let during_turn = VALID_EVENTS.replacen("{\"type\":\"item.completed\"", &(metadata_warning("mid_warning") + "{\"type\":\"item.completed\""), 1);
    assert_eq!(decode(&during_turn).unwrap_err().code, "codex_tool_event");
}
fn request(model: ModelSelection) -> CodexRequest {
    CodexRequest { question: "Объясни SYNTHETIC_QUESTION".into(), code: Some("print('SYNTHETIC_CODE')".into()), statement: None, model, gateway: None }
}
fn decode(events: &str) -> Result<String, CodexError> {
    let mut decoder = JsonlDecoder::default(); decoder.feed(events.as_bytes())?; decoder.finish(0)
}
fn features() -> String {
    DENIED_FEATURES.iter().map(|f| format!("{f}\tstable\tfalse\n"))
        .chain(["unified_exec\tstable\ttrue\n".to_owned(), "item_ids\tremoved\ttrue\n".to_owned()]).collect()
}

#[test]
fn jsonl_handles_every_utf8_byte_boundary_and_unterminated_final_line() {
    for split in 0..=VALID_EVENTS.len() {
        let mut decoder = JsonlDecoder::default();
        decoder.feed(&VALID_EVENTS.as_bytes()[..split]).unwrap();
        decoder.feed(&VALID_EVENTS.as_bytes()[split..]).unwrap();
        assert_eq!(decoder.finish(0).unwrap(), "Ответ ✓");
    }
    assert_eq!(decode(VALID_EVENTS.trim_end()).unwrap(), "Ответ ✓");
}

#[test]
fn reasoning_never_enters_answer_and_updates_replace_instead_of_duplicate() {
    let events = concat!(
        "{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}\n",
        "{\"type\":\"turn.started\"}\n",
        "{\"type\":\"item.completed\",\"item\":{\"id\":\"r\",\"type\":\"reasoning\",\"text\":\"PRIVATE_REASONING\"}}\n",
        "{\"type\":\"item.started\",\"item\":{\"id\":\"a\",\"type\":\"agent_message\"}}\n",
        "{\"type\":\"item.updated\",\"item\":{\"id\":\"a\",\"type\":\"agent_message\",\"text\":\"Hel\"}}\n",
        "{\"type\":\"item.completed\",\"item\":{\"id\":\"a\",\"type\":\"agent_message\",\"text\":\"Hello\"}}\n",
        "{\"type\":\"turn.completed\"}\n",
    );
    assert_eq!(decode(events).unwrap(), "Hello");
}

#[test]
fn rejects_tool_items_unknown_items_and_nested_calls_before_success() {
    for kind in ["command_execution", "file_change", "mcp_tool_call", "web_search", "function_call", "image_view", "unexpected"] {
        let events = format!("{{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}}\n{{\"type\":\"turn.started\"}}\n{{\"type\":\"item.completed\",\"item\":{{\"id\":\"x\",\"type\":\"{kind}\"}}}}\n");
        assert_eq!(decode(&events).unwrap_err().code, "codex_tool_event");
    }
    let events = "{\"type\":\"turn.started\",\"metadata\":{\"tool_calls\":[]}}\n";
    assert_eq!(decode(events).unwrap_err().code, "codex_tool_event");
    let after = format!("{VALID_EVENTS}{{\"type\":\"tool.call\"}}\n");
    assert_eq!(decode(&after).unwrap_err().code, "codex_tool_event");
}

#[test]
fn rejects_invalid_truncated_empty_out_of_order_and_nonzero_protocols() {
    for events in ["not json\n", "{}\n", "{\"type\":\"turn.completed\"}\n", "{\"type\":\"turn.started\"}\n", "{\"type\":\"turn.started\"}\n{\"type\":\"turn.completed\"}\n"] {
        assert_eq!(decode(events).unwrap_err().code, "codex_protocol");
    }
    let unfinished = "{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}\n{\"type\":\"turn.started\"}\n{\"type\":\"item.updated\",\"item\":{\"id\":\"a\",\"type\":\"agent_message\",\"text\":\"unfinished\"}}\n{\"type\":\"turn.completed\"}\n";
    assert_eq!(decode(unfinished).unwrap_err().code, "codex_protocol");
    let mut decoder = JsonlDecoder::default(); decoder.feed(VALID_EVENTS.as_bytes()).unwrap();
    assert_eq!(decoder.finish(1).unwrap_err().code, "codex_process_failed");
}

#[test]
fn requires_thread_start_and_rejects_duplicate_keys_at_all_depths() {
    let without_thread = VALID_EVENTS.lines().skip(1).collect::<Vec<_>>().join("\n");
    assert_eq!(decode(&without_thread).unwrap_err().code, "codex_protocol");
    for event in [
        r#"{"type":"thread.started","type":"turn.started","thread_id":"synthetic"}"#,
        r#"{"type":"item.completed","item":{"type":"command_execution","type":"agent_message","id":"a","text":"hidden"}}"#,
        r#"{"type":"item.completed","item":{"type":"agent_message","id":"a","text":"x","metadata":{"tool":true,"tool":false}}}"#,
    ] {
        let events = format!("{{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}}\n{{\"type\":\"turn.started\"}}\n{event}\n");
        assert_eq!(decode(&events).unwrap_err().code, "codex_protocol");
    }
    assert_eq!(decode("{\"type\":\"thread.started\"}\n").unwrap_err().code, "codex_protocol");
}

#[test]
fn deeply_nested_json_fails_with_fixed_protocol_error_before_recursive_walk() {
    let event = format!("{{\"type\":\"thread.started\",\"thread_id\":\"synthetic\",\"metadata\":{}0{}}}\n", "[".repeat(1000), "]".repeat(1000));
    assert_eq!(decode(&event).unwrap_err().code, "codex_protocol");
}

#[test]
fn protocol_errors_are_fixed_and_never_echo_cli_messages() {
    let error = decode("{\"type\":\"error\",\"message\":\"SYNTHETIC_AUTH_URL_AND_CODE\"}\n").unwrap_err();
    assert_eq!(error.code, "codex_process_failed");
    assert!(!serde_json::to_string(&error).unwrap().contains("SYNTHETIC_AUTH"));
}

#[test]
fn output_and_line_limits_fail_before_unbounded_buffering() {
    let mut decoder = JsonlDecoder::default();
    assert_eq!(decoder.feed(&vec![b'x'; MAX_LINE_BYTES + 1]).unwrap_err().code, "codex_output_limit");
    let mut decoder = JsonlDecoder::default();
    assert_eq!(decoder.feed(&vec![b' '; MAX_STDOUT_BYTES + 1]).unwrap_err().code, "codex_output_limit");
    let message = serde_json::json!({"type":"item.completed","item":{"id":"a","type":"agent_message","text":"x".repeat(MAX_ANSWER_BYTES+1)}});
    let events = format!("{{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}}\n{{\"type\":\"turn.started\"}}\n{message}\n");
    assert_eq!(decode(&events).unwrap_err().code, "codex_output_limit");
}

#[test]
fn input_limits_and_model_selection_are_explicit() {
    assert!(validate_request(&request(ModelSelection::CliDefault)).is_ok());
    let mut input = request(ModelSelection::CliDefault);
    input.question = " ".into(); assert_eq!(validate_request(&input).unwrap_err().code, "codex_request_invalid");
    input.question = "x".repeat(MAX_QUESTION_BYTES + 1); assert!(validate_request(&input).is_err());
    input.question = "ok".into(); input.code = Some("x".repeat(MAX_CODE_BYTES + 1)); assert!(validate_request(&input).is_err());
    input.code = None; input.statement = Some("x".repeat(MAX_STATEMENT_BYTES + 1)); assert!(validate_request(&input).is_err());
    input.statement = None; input.question = "null\0text".into(); assert!(validate_request(&input).is_err());
    for id in ["", "x\ny", "--model bad", "--dangerously-bypass-approvals-and-sandbox", "$(COMMAND)"] {
        assert!(validate_request(&request(ModelSelection::Manual { id: id.into() })).is_err());
    }
    assert!(validate_request(&request(ModelSelection::Manual { id: "provider/model-1.2".into() })).is_ok());
    assert!(serde_json::from_str::<CodexRequest>(r#"{"question":"x","code":null,"statement":null}"#).is_err());
    assert!(serde_json::from_str::<CodexRequest>(r#"{"question":"x","model":{"mode":"cliDefault"},"filePath":"secret"}"#).is_err());
}

#[test]
fn args_enforce_safety_and_never_substitute_manual_model() {
    let default_args: Vec<String> = request_args(&ModelSelection::CliDefault).iter().map(|s| s.to_string_lossy().into_owned()).collect();
    for flag in ["--strict-config", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"] { assert!(default_args.iter().any(|s| s == flag)); }
    assert!(default_args.windows(2).any(|w| w == ["--sandbox", "read-only"]));
    assert!(default_args.windows(2).any(|w| w == ["--ask-for-approval", "never"]));
    assert!(default_args.windows(2).any(|w| w == ["--disable", "shell_tool"]));
    assert_eq!(default_args.last().unwrap(), "-"); assert!(!default_args.iter().any(|s| s == "--model"));
    assert!(!default_args.iter().any(|s| s.contains("tools.exec") || s.contains("tools.apply_patch")));
    let manual: Vec<_> = request_args(&ModelSelection::Manual { id: "explicit-model".into() }).iter().map(|s| s.to_string_lossy().into_owned()).collect();
    assert!(manual.windows(2).any(|w| w == ["--model", "explicit-model"]));
}

#[test]
fn feature_proof_requires_every_deny_and_rejects_unknown_enabled_flags() {
    assert!(verify_features(&features()).is_ok());
    assert!(verify_features(&features().replace("shell_tool\tstable\tfalse", "shell_tool\tstable\ttrue")).is_err());
    assert!(verify_features(&(features() + "new_tool\tstable\ttrue\n")).is_err());
    assert!(verify_features(&(features() + "shell_tool\tstable\tfalse\n")).is_err());
    assert!(verify_features(&features().replace("item_ids\tremoved\ttrue", "item_ids\tstable\ttrue")).is_err());
    assert!(verify_features(&features().replace("unified_exec\tstable\ttrue", "unified_exec\tremoved\ttrue")).is_err());
    assert!(verify_features("shell_tool stable false\n").is_err());
    assert!(verify_features("malformed\n").is_err());
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Scenario { Good, BadVersion, BadFeature, Tool, Nonzero, Overflow, HangRequest, HangProbe, HangLogin, SpawnFailure, CancelFailure, SlowCancel }
struct Recorded { args: Vec<String>, stdin: Vec<u8>, env_keys: Vec<String>, cwd_empty: bool, home: PathBuf, data_roots: Vec<PathBuf> }
struct FakeBackend {
    scenario: Scenario, calls: Mutex<Vec<Recorded>>,
    cancellations: Arc<AtomicUsize>, drops: Arc<AtomicUsize>,
}
impl FakeBackend {
    fn new(scenario: Scenario) -> Arc<Self> {
        Arc::new(Self { scenario, calls: Mutex::new(vec![]), cancellations: Arc::new(AtomicUsize::new(0)), drops: Arc::new(AtomicUsize::new(0)) })
    }
    fn count(&self, arg: &str) -> usize { self.calls.lock().unwrap().iter().filter(|c| c.args.iter().any(|a| a == arg)).count() }
}
struct FakeProcess { chunks: Vec<Vec<u8>>, index: usize, exit: u32, hang: bool, overflow: bool, fail_cancel: bool, slow_cancel: bool, cancellations: Arc<AtomicUsize>, drops: Arc<AtomicUsize> }
impl RunningProcess for FakeProcess {
    fn poll(&mut self) -> Result<ProcessSnapshot, ()> {
        let stdout = self.chunks.get(self.index).cloned().unwrap_or_default(); self.index += 1;
        Ok(ProcessSnapshot { stdout, stderr_bytes: 0, exit_code: if self.hang || self.index < self.chunks.len() { None } else { Some(self.exit) }, output_overflow: self.overflow })
    }
    fn cancel(&mut self) -> Result<(), ()> {
        self.cancellations.fetch_add(1, Ordering::SeqCst);
        if self.fail_cancel { return Err(()); }
        if self.slow_cancel { std::thread::sleep(Duration::from_millis(120)); }
        self.hang = false; Ok(())
    }
}
impl Drop for FakeProcess { fn drop(&mut self) { self.drops.fetch_add(1, Ordering::SeqCst); } }
impl ProcessBackend for FakeBackend {
    fn spawn(&self, spec: ProcessSpec) -> Result<Box<dyn RunningProcess>, ()> {
        let args: Vec<String> = spec.args.iter().map(|s| s.to_string_lossy().into_owned()).collect();
        let is_request = args.iter().any(|a| a == "exec") && !args.iter().any(|a| a == "--help");
        let is_login = args.iter().any(|a| a == "login");
        let is_version = args.iter().any(|a| a == "--version");
        let is_features = args.iter().any(|a| a == "features");
        let env_keys = spec.env.iter().map(|(key, _)| key.to_string_lossy().into_owned()).collect();
        let home = PathBuf::from(&spec.env.iter().find(|(k, _)| k == "CODEX_HOME").unwrap().1);
        let data_roots = spec.env.iter().filter(|(key, _)| key == "APPDATA" || key == "LOCALAPPDATA").map(|(_, value)| PathBuf::from(value)).collect();
        self.calls.lock().unwrap().push(Recorded {
            args, stdin: spec.stdin, env_keys, home, data_roots,
            cwd_empty: std::fs::read_dir(&spec.cwd).unwrap().next().is_none(),
        });
        if self.scenario == Scenario::SpawnFailure { return Err(()); }
        let output = if is_version {
            if self.scenario == Scenario::BadVersion { "codex-cli 0.153.0-alpha.5\n".into() }
            else { format!("codex-cli {SUPPORTED_CLI_VERSION}\n") }
        } else if is_features {
            if self.scenario == Scenario::BadFeature { features().replace("shell_tool\tstable\tfalse", "shell_tool\tstable\ttrue") } else { features() }
        } else if is_request {
            if self.scenario == Scenario::Tool { "{\"type\":\"thread.started\",\"thread_id\":\"synthetic\"}\n{\"type\":\"turn.started\"}\n{\"type\":\"item.started\",\"item\":{\"type\":\"command_execution\",\"command\":\"SYNTHETIC\"}}\n".into() } else { VALID_EVENTS.into() }
        } else if is_login { "SYNTHETIC_PRIVATE_AUTH_MUST_NOT_SURFACE\n".into() }
        else { "--strict-config --sandbox read-only --ask-for-approval never --disable --config --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --json --color never --model stdin".into() };
        let hang = (is_request && matches!(self.scenario, Scenario::HangRequest | Scenario::CancelFailure | Scenario::SlowCancel)) || (is_version && self.scenario == Scenario::HangProbe) || (is_login && self.scenario == Scenario::HangLogin);
        let bytes = output.into_bytes(); let half = bytes.len() / 2;
        Ok(Box::new(FakeProcess {
            chunks: if hang { vec![] } else { vec![bytes[..half].to_vec(), bytes[half..].to_vec()] }, index: 0,
            exit: if is_request && self.scenario == Scenario::Nonzero { 1 } else { 0 },
            hang, overflow: is_request && self.scenario == Scenario::Overflow,
            fail_cancel: is_request && self.scenario == Scenario::CancelFailure,
            slow_cancel: is_request && self.scenario == Scenario::SlowCancel,
            cancellations: Arc::clone(&self.cancellations), drops: Arc::clone(&self.drops),
        }))
    }
}

static TEST_ID: AtomicUsize = AtomicUsize::new(1);
struct TestRoot(PathBuf);
impl TestRoot {
    fn new() -> Self { Self(std::env::temp_dir().join(format!("kontur-codex-test-{}-{}", std::process::id(), TEST_ID.fetch_add(1, Ordering::Relaxed)))) }
}
impl Drop for TestRoot {
    fn drop(&mut self) {
        // Only known empty synthetic directories. Never recursive deletion.
        for name in ["local-appdata", "roaming-appdata"] { let _ = std::fs::remove_dir(self.0.join("codex-temp").join(name)); }
        for name in ["codex-home", "codex-temp", "codex-requests"] { let _ = std::fs::remove_dir(self.0.join(name)); }
        let _ = std::fs::remove_dir(&self.0);
    }
}
fn service(root: &TestRoot, backend: Arc<FakeBackend>, timeout: Duration) -> CodexService {
    CodexService::with_backend(root.0.clone(), backend, Limits { probe: timeout, request: timeout, login: timeout, poll: Duration::from_millis(1), shutdown: Duration::from_millis(30), inactivity: Duration::from_millis(30) }, Some(root.0.join("fake-codex.exe")))
}

fn tutor_request() -> CodexRequest {
    CodexRequest { question: "Kontur tutor request".into(), code: None, statement: None,
        model: ModelSelection::Manual { id: "gpt-6.1-sol".into() },
        gateway: Some(GatewayRequest { effort: "high".into(), rendered_prompt: serde_json::json!({
            "trusted_metadata":{"synthetic":true},
            "untrusted_sections":[{"name":"USER_REQUEST","content":"SYNTHETIC_CONTEXT_".repeat(2000)}],
        }).to_string() }),
    }
}

#[test]
fn gateway_keeps_complete_canonical_input_and_exact_manual_settings() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::Good);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let input = tutor_request(); let expected = input.gateway.as_ref().unwrap().rendered_prompt.as_bytes().to_vec();
    assert!(expected.len() > MAX_QUESTION_BYTES);
    assert_eq!(service.status().gateway_protocol, 1);
    wait_until(|| service.status().state == StatusState::Ready);
    let started = service.start(input).unwrap();
    let result = terminal(&service, &started.request_id);
    assert_eq!(result.state, RequestState::Completed); assert_eq!(result.effort.as_deref(), Some("high"));
    let calls = backend.calls.lock().unwrap();
    let call = calls.iter().find(|c| !c.stdin.is_empty()).unwrap();
    assert_eq!(call.stdin, expected);
    assert!(call.args.windows(2).any(|w| w == ["--model", "gpt-6.1-sol"]));
    assert!(call.args.iter().any(|a| a == "model_reasoning_effort=\"high\""));
    assert!(call.args.iter().any(|a| a.starts_with("developer_instructions=") && a.contains("untrusted_sections")));
    assert!(!call.args.iter().any(|a| a.contains("SYNTHETIC_CONTEXT")));
    drop(calls); service.shutdown().unwrap();
}

#[test]
fn gateway_rejects_lossy_or_ambiguous_context_and_unsupported_effort() {
    let mut input = tutor_request(); input.code = Some("print(1)".into()); assert!(validate_request(&input).is_err());
    input.code = None; input.model = ModelSelection::CliDefault; assert!(validate_request(&input).is_err());
    input.model = ModelSelection::Manual { id: "gpt-6.1-sol".into() };
    input.gateway.as_mut().unwrap().effort = "auto".into(); assert!(validate_request(&input).is_err());
    input.gateway.as_mut().unwrap().effort = "low".into();
    input.gateway.as_mut().unwrap().rendered_prompt = "x".repeat(500_001); assert!(validate_request(&input).is_err());
}

#[test]
fn gateway_idle_deadline_cancels_owned_process_before_wall_deadline() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::HangRequest);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let before = Instant::now(); let started = service.start(tutor_request()).unwrap();
    let result = terminal(&service, &started.request_id);
    assert_eq!(result.state, RequestState::Failed); assert_eq!(result.error.unwrap().code, "codex_timeout");
    assert!(before.elapsed() < Duration::from_millis(500));
    assert_eq!(backend.cancellations.load(Ordering::SeqCst), 1);
    service.shutdown().unwrap();
}
fn wait_until(mut predicate: impl FnMut() -> bool) {
    let started = Instant::now();
    while !predicate() { assert!(started.elapsed() < Duration::from_secs(3), "fake executor did not settle"); std::thread::sleep(Duration::from_millis(1)); }
}
fn terminal(service: &CodexService, id: &str) -> CodexPoll {
    wait_until(|| !matches!(service.poll(id).unwrap().state, RequestState::Starting | RequestState::Running));
    service.poll(id).unwrap()
}

#[test]
fn constructor_is_idle_status_is_local_and_ready_contains_no_auth_output() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::Good);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    assert!(backend.calls.lock().unwrap().is_empty()); assert!(!root.0.exists());
    service.status(); wait_until(|| service.status().state == StatusState::Ready);
    assert_eq!(backend.count("exec"), 1); // exec --help only, no stdin/model request
    assert_eq!(backend.calls.lock().unwrap().iter().filter(|c| !c.stdin.is_empty()).count(), 0);
    assert_eq!(backend.count("login"), 0);
    assert_eq!(service.status().login_state, LoginState::Unknown);
    assert!(service.status().manual_models.is_empty()); service.shutdown().unwrap();
}

#[test]
fn user_request_uses_stdin_empty_cwd_dedicated_home_and_replacement_environment() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::Good);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let result = service.start(request(ModelSelection::CliDefault)).unwrap();
    let result = terminal(&service, &result.request_id);
    assert_eq!(result.state, RequestState::Completed); assert_eq!(result.text, "Ответ ✓");
    let calls = backend.calls.lock().unwrap();
    assert_eq!(calls.len(), 5); // fresh version, help, exec help, feature proof, one request
    for call in calls.iter() {
        assert!(call.cwd_empty); assert_eq!(call.home, root.0.join("codex-home"));
        assert_eq!(call.data_roots.len(), 2); assert!(call.data_roots.iter().all(|path| path.starts_with(root.0.join("codex-temp"))));
        for key in &call.env_keys { assert!(["SystemRoot", "WINDIR", "LOCALAPPDATA", "APPDATA", "PATH", "CODEX_HOME", "TEMP", "TMP"].contains(&key.as_str())); }
        assert!(!call.args.iter().any(|arg| arg.contains("SYNTHETIC_")));
    }
    let input: serde_json::Value = serde_json::from_slice(&calls.last().unwrap().stdin).unwrap();
    assert_eq!(input["selectedCode"], "print('SYNTHETIC_CODE')"); assert!(input["selectedStatement"].is_null());
    drop(calls); service.shutdown().unwrap();
}

#[test]
fn unverified_teardown_stays_unavailable_and_cannot_be_reopened_by_shutdown() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::CancelFailure);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let started = service.start(request(ModelSelection::CliDefault)).unwrap();
    wait_until(|| backend.calls.lock().unwrap().len() == 5);
    service.cancel(&started.request_id).unwrap(); let result = terminal(&service, &started.request_id);
    assert_eq!(result.state, RequestState::Failed); assert_eq!(result.error.unwrap().code, "codex_cancel_failed");
    let calls = backend.calls.lock().unwrap().len();
    assert_eq!(service.status().state, StatusState::Unavailable);
    assert!(service.start(request(ModelSelection::CliDefault)).is_err()); assert!(service.login().is_err());
    assert_eq!(service.shutdown().unwrap_err().code, "codex_cancel_failed");
    assert_eq!(service.shutdown().unwrap_err().code, "codex_cancel_failed"); service.status();
    assert_eq!(backend.calls.lock().unwrap().len(), calls);
}

#[test]
fn shutdown_deadline_remains_failed_after_late_successful_worker_cleanup() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::SlowCancel);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let started = service.start(request(ModelSelection::CliDefault)).unwrap();
    wait_until(|| backend.calls.lock().unwrap().len() == 5);
    let clock = Instant::now(); assert_eq!(service.shutdown().unwrap_err().code, "codex_cancel_failed");
    assert!(clock.elapsed() < Duration::from_millis(100));
    assert_eq!(terminal(&service, &started.request_id).state, RequestState::Cancelled);
    assert_eq!(backend.drops.load(Ordering::SeqCst), 5);
    assert_eq!(service.shutdown().unwrap_err().code, "codex_cancel_failed");
    assert_eq!(service.status().error.unwrap().code, "codex_cancel_failed");
    assert!(service.start(request(ModelSelection::CliDefault)).is_err()); assert!(service.login().is_err());
}

#[test]
fn incompatibility_and_feature_failure_never_start_a_model_request() {
    for scenario in [Scenario::BadVersion, Scenario::BadFeature, Scenario::SpawnFailure] {
        let root = TestRoot::new(); let backend = FakeBackend::new(scenario);
        let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
        let started = service.start(request(ModelSelection::CliDefault)).unwrap();
        assert_eq!(terminal(&service, &started.request_id).state, RequestState::Failed);
        assert!(!backend.calls.lock().unwrap().iter().any(|c| !c.stdin.is_empty())); service.shutdown().unwrap();
    }
}

#[test]
fn invalid_input_never_spawns_and_busy_request_cannot_be_replaced() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::HangRequest);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let mut invalid = request(ModelSelection::CliDefault); invalid.question.clear();
    assert_eq!(service.start(invalid).unwrap_err().code, "codex_request_invalid"); assert!(backend.calls.lock().unwrap().is_empty());
    let started = service.start(request(ModelSelection::CliDefault)).unwrap();
    assert_eq!(service.start(request(ModelSelection::CliDefault)).unwrap_err().code, "codex_busy");
    service.cancel(&started.request_id).unwrap(); assert_eq!(terminal(&service, &started.request_id).state, RequestState::Cancelled);
    assert_eq!(service.cancel(&started.request_id).unwrap().state, RequestState::Cancelled);
    assert_eq!(service.cancel("unknown").unwrap_err().code, "codex_not_running"); service.shutdown().unwrap();
}

#[test]
fn manual_model_failure_retains_choice_and_never_retries_a_default() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::Nonzero);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let model = ModelSelection::Manual { id: "explicit-unavailable-model".into() };
    let started = service.start(request(model.clone())).unwrap(); let result = terminal(&service, &started.request_id);
    assert_eq!(result.state, RequestState::Failed); assert_eq!(result.model, model);
    assert_eq!(result.error.unwrap().code, "codex_process_failed");
    let calls = backend.calls.lock().unwrap(); assert_eq!(calls.len(), 5);
    assert!(calls.last().unwrap().args.windows(2).any(|w| w == ["--model", "explicit-unavailable-model"]));
    drop(calls); service.shutdown().unwrap();
}

#[test]
fn tool_and_overflow_failures_cancel_the_owned_process() {
    for (scenario, code) in [(Scenario::Tool, "codex_tool_event"), (Scenario::Overflow, "codex_output_limit")] {
        let root = TestRoot::new(); let backend = FakeBackend::new(scenario);
        let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
        let started = service.start(request(ModelSelection::CliDefault)).unwrap(); let result = terminal(&service, &started.request_id);
        assert_eq!(result.state, RequestState::Failed); assert_eq!(result.error.unwrap().code, code);
        assert_eq!(backend.cancellations.load(Ordering::SeqCst), 1); assert_eq!(backend.drops.load(Ordering::SeqCst), 5);
        service.shutdown().unwrap();
    }
}

#[test]
fn timeout_and_explicit_cancel_wait_for_process_drop() {
    for explicit in [false, true] {
        let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::HangRequest);
        let service = service(&root, Arc::clone(&backend), Duration::from_millis(35));
        let started = service.start(request(ModelSelection::CliDefault)).unwrap();
        wait_until(|| backend.calls.lock().unwrap().len() == 5);
        if explicit { service.cancel(&started.request_id).unwrap(); }
        let result = terminal(&service, &started.request_id);
        assert_eq!(result.state, if explicit { RequestState::Cancelled } else { RequestState::Failed });
        assert_eq!(result.error.unwrap().code, if explicit { "codex_cancelled" } else { "codex_timeout" });
        assert_eq!(backend.cancellations.load(Ordering::SeqCst), 1); assert_eq!(backend.drops.load(Ordering::SeqCst), 5);
        service.shutdown().unwrap();
    }
}

#[test]
fn shutdown_cancels_request_login_and_probe_before_returning_and_rejects_new_work() {
    for scenario in [Scenario::HangRequest, Scenario::HangLogin, Scenario::HangProbe] {
        let root = TestRoot::new(); let backend = FakeBackend::new(scenario);
        let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
        match scenario {
            Scenario::HangRequest => { service.start(request(ModelSelection::CliDefault)).unwrap(); }
            Scenario::HangLogin => { service.login().unwrap(); }
            _ => { service.status(); }
        }
        let expected = if scenario == Scenario::HangProbe { 1 } else { 5 };
        wait_until(|| backend.calls.lock().unwrap().len() == expected);
        let started = Instant::now(); service.shutdown().unwrap(); assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(backend.cancellations.load(Ordering::SeqCst), 1); assert_eq!(backend.drops.load(Ordering::SeqCst), expected);
        let calls = backend.calls.lock().unwrap().len(); service.status();
        assert!(service.start(request(ModelSelection::CliDefault)).is_err()); assert!(service.login().is_err());
        assert_eq!(backend.calls.lock().unwrap().len(), calls);
    }
}

#[test]
fn login_only_on_action_keeps_private_output_out_of_status_and_request_text() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::Good);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    service.login().unwrap(); wait_until(|| service.status().login_state == LoginState::Ready);
    let json = serde_json::to_string(&service.status()).unwrap(); assert!(!json.contains("SYNTHETIC_PRIVATE_AUTH"));
    assert_eq!(backend.count("login"), 1); assert!(service.poll("unknown").is_err()); service.shutdown().unwrap();
}

#[test]
fn successful_idle_shutdown_rearms_once_and_only_explicit_send_starts_work() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::Good);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let permit = service.shutdown().unwrap();
    assert!(backend.calls.lock().unwrap().is_empty());
    service.resume_after_aborted_close(&permit).unwrap();
    assert!(backend.calls.lock().unwrap().is_empty());
    assert!(service.resume_after_aborted_close(&permit).is_err());
    let started = service.start(request(ModelSelection::CliDefault)).unwrap();
    assert_eq!(terminal(&service, &started.request_id).state, RequestState::Completed);
    assert_eq!(backend.calls.lock().unwrap().len(), 5);
    service.shutdown().unwrap();
    assert!(service.resume_after_aborted_close(&permit).is_err());
}

#[test]
fn request_login_and_probe_resume_after_confirmed_shutdown_without_new_calls() {
    for scenario in [Scenario::HangRequest, Scenario::HangLogin, Scenario::HangProbe] {
        let root = TestRoot::new(); let backend = FakeBackend::new(scenario);
        let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
        let request_id = match scenario {
            Scenario::HangRequest => Some(service.start(request(ModelSelection::Manual { id: "chosen-model".into() })).unwrap().request_id),
            Scenario::HangLogin => { service.login().unwrap(); None }
            _ => { service.status(); None }
        };
        let expected = if scenario == Scenario::HangProbe { 1 } else { 5 };
        wait_until(|| backend.calls.lock().unwrap().len() == expected);
        let permit = service.shutdown().unwrap();
        let prior = request_id.as_ref().map(|id| serde_json::to_value(service.poll(id).unwrap()).unwrap());
        let cancellations = backend.cancellations.load(Ordering::SeqCst);
        service.resume_after_aborted_close(&permit).unwrap();
        assert_eq!(backend.calls.lock().unwrap().len(), expected);
        assert_eq!(backend.cancellations.load(Ordering::SeqCst), cancellations);
        if let Some(id) = request_id {
            let retained = service.poll(&id).unwrap();
            assert_eq!(retained.state, RequestState::Cancelled);
            assert_eq!(serde_json::to_value(retained).unwrap(), prior.unwrap());
        }
        service.shutdown().unwrap();
    }
}

#[test]
fn close_permits_cannot_cross_services_or_resume_a_later_shutdown() {
    let root_a = TestRoot::new(); let backend_a = FakeBackend::new(Scenario::Good);
    let root_b = TestRoot::new(); let backend_b = FakeBackend::new(Scenario::Good);
    let service_a = service(&root_a, Arc::clone(&backend_a), Duration::from_secs(1));
    let service_b = service(&root_b, Arc::clone(&backend_b), Duration::from_secs(1));
    let old = service_a.shutdown().unwrap();
    let current = service_a.shutdown().unwrap();
    let other = service_b.shutdown().unwrap();
    assert!(service_a.resume_after_aborted_close(&old).is_err());
    assert!(service_a.resume_after_aborted_close(&other).is_err());
    assert!(service_b.resume_after_aborted_close(&current).is_err());
    assert!(service_a.start(request(ModelSelection::CliDefault)).is_err());
    assert!(service_b.start(request(ModelSelection::CliDefault)).is_err());
    service_a.resume_after_aborted_close(&current).unwrap();
    service_b.resume_after_aborted_close(&other).unwrap();
    assert!(backend_a.calls.lock().unwrap().is_empty()); assert!(backend_b.calls.lock().unwrap().is_empty());
    service_a.shutdown().unwrap(); service_b.shutdown().unwrap();
}

#[test]
fn stale_permit_cannot_rearm_or_cancel_an_active_new_request() {
    let root = TestRoot::new(); let backend = FakeBackend::new(Scenario::HangRequest);
    let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
    let permit = service.shutdown().unwrap(); service.resume_after_aborted_close(&permit).unwrap();
    let started = service.start(request(ModelSelection::CliDefault)).unwrap();
    wait_until(|| backend.calls.lock().unwrap().len() == 5);
    assert!(service.resume_after_aborted_close(&permit).is_err());
    assert_eq!(backend.cancellations.load(Ordering::SeqCst), 0);
    assert_eq!(service.poll(&started.request_id).unwrap().state, RequestState::Running);
    service.shutdown().unwrap();
}

#[test]
fn rearm_never_clears_cancel_failure_or_shutdown_timeout_after_late_cleanup() {
    for scenario in [Scenario::CancelFailure, Scenario::SlowCancel] {
        let root = TestRoot::new(); let backend = FakeBackend::new(scenario);
        let service = service(&root, Arc::clone(&backend), Duration::from_secs(1));
        let prior = service.shutdown().unwrap(); service.resume_after_aborted_close(&prior).unwrap();
        let started = service.start(request(ModelSelection::CliDefault)).unwrap();
        wait_until(|| backend.calls.lock().unwrap().len() == 5);
        if scenario == Scenario::CancelFailure {
            service.cancel(&started.request_id).unwrap(); terminal(&service, &started.request_id);
        }
        assert_eq!(service.shutdown().unwrap_err().code, "codex_cancel_failed");
        terminal(&service, &started.request_id);
        let calls = backend.calls.lock().unwrap().len();
        assert_eq!(service.resume_after_aborted_close(&prior).unwrap_err().code, "codex_cancel_failed");
        assert!(service.start(request(ModelSelection::CliDefault)).is_err()); assert!(service.login().is_err());
        assert_eq!(backend.calls.lock().unwrap().len(), calls);
        assert_eq!(service.shutdown().unwrap_err().code, "codex_cancel_failed");
    }
}
