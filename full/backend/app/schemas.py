"""Pydantic request contracts for API v1."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FixtureAssetIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    filename: str | None = None
    name: str | None = None
    media_type: str = "application/octet-stream"
    content_base64: str | None = None
    content_hash: str | None = None
    role: str = Field(default="file", pattern="^(file|image)$")


class FixtureTaskIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_url: str
    source_id: str | None = None
    external_id: str | None = None
    line_number: int
    exam_year: int | None = None
    target_exam_year: int | None = Field(default=None, ge=2000, le=2100)
    statement: str
    statement_latex: str | None = None
    answer: str | None = None
    explanation: str | None = None
    rights_status: str = "review_required"
    assets: list[FixtureAssetIn] = Field(default_factory=list, max_length=20)


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    pinned: bool = False
    archived: bool = False
    order_index: int = 0


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    pinned: bool | None = None
    archived: bool | None = None
    order_index: int | None = None


class ConversationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str | None = None
    title: str | None = Field(default=None, max_length=255)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str | None = None
    title: str | None = Field(default=None, max_length=255)
    pinned: bool | None = None
    archived: bool | None = None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str = Field(default="user", pattern="^(user|assistant|system)$")
    content: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SolveSessionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_revision_id: str
    conversation_id: str | None = None


class BookmarkPut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_revision_id: str = Field(min_length=1, max_length=255)


class CheckAnswerIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: str = Field(max_length=20_000)


class CheckAnswerOut(BaseModel):
    attempt_id: str | None
    status: Literal["correct", "incorrect", "unavailable"]
    correct: bool | None
    reason: str | None


class CodePut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str = Field(max_length=200_000)
    language: str = Field(default="python", max_length=32)
    version: int = Field(default=0, ge=0)


class AnnotationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str = Field(pattern="^(text|ink)$")
    payload: dict[str, Any] = Field(default_factory=dict)
    anchor: dict[str, Any] = Field(default_factory=dict)


class BoardPut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    snapshot: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)


class AttemptIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = Field(default="in_progress", pattern="^(in_progress|solved|failed|self_reported)$")
    user_answer: str | None = Field(default=None, max_length=20_000)
    verified_correct: bool | None = None
    verification_method: str | None = Field(default=None, max_length=64)
    independent: bool = True
    hint_count: int = Field(default=0, ge=0, le=100)
    error_type: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=20_000)


class AttemptPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str | None = Field(default=None, pattern="^(in_progress|solved|failed|self_reported)$")
    user_answer: str | None = Field(default=None, max_length=20_000)
    verified_correct: bool | None = None
    verification_method: str | None = Field(default=None, max_length=64)
    independent: bool | None = None
    hint_count: int | None = Field(default=None, ge=0, le=100)
    error_type: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=20_000)


class CodeSampleIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1, max_length=255)
    language: str = Field(default="python", max_length=32)
    content: str = Field(min_length=1, max_length=200_000)
    task_lines: list[int] = Field(default_factory=list, max_length=27)
    tags: list[str] = Field(default_factory=list, max_length=30)
    usage_note: str = Field(default="", max_length=2000)
    enabled: bool = True


class CodeSamplePatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1, max_length=200_000)
    task_lines: list[int] | None = Field(default=None, max_length=27)
    tags: list[str] | None = Field(default=None, max_length=30)
    usage_note: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class ContextPreviewIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    message: str = Field(min_length=1, max_length=100_000)
    model_selection: str = "auto"
    effort: str | None = None
    tutor_mode: str = "hint"
    solve_session_id: str | None = None
    selected_sample_ids: list[str] = Field(default_factory=list, max_length=3)


class AssistantRunIn(ContextPreviewIn):
    pass


class SolutionComparisonIn(BaseModel):
    """Typed input for the comparison alias over the assistant pipeline."""

    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    message: str = Field(min_length=1, max_length=100_000)
    model_selection: str = "auto"
    effort: str | None = None
    solve_session_id: str | None = None
    selected_sample_ids: list[str] = Field(default_factory=list, max_length=3)


class ErrorIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: str | None = None
    error_type: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=20_000)
    prevention_note: str = Field(default="", max_length=20_000)
    source: str = Field(default="user", max_length=64)
    status: str = Field(default="proposed", pattern="^(proposed|confirmed|rejected)$")


class ErrorPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = Field(pattern="^(proposed|confirmed|rejected)$")
    description: str | None = Field(default=None, max_length=20_000)
    prevention_note: str | None = Field(default=None, max_length=20_000)


class ReviewCompleteIn(BaseModel):
    success: bool


class BlueprintIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    exam_year: int | None = Field(default=None, ge=2021, le=2100)
    required_lines: dict[str, int]
    constraints: dict[str, Any] = Field(default_factory=dict)


class ExamIn(BaseModel):
    blueprint_id: str
    seed: int
    mode: Literal["strict", "study"] | None = None


class ExamPrepareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["full", "thematic", "custom", "errors"]
    mode: Literal["strict", "study"]
    required_lines: dict[str, int] = Field(default_factory=dict)
    difficulty: Literal["any", "easy", "medium", "hard"] = "any"
    duration_minutes: int = Field(ge=1, le=24 * 60)
    exclude_solved: bool = False
    exclude_recent: bool = False
    source_year: int | None = Field(default=None, ge=2000, le=2100)
    include_corrected_errors: bool = False


class VariantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    difficulty: Literal["basic", "medium", "hard"]
    seed: int = Field(ge=0, le=4_294_967_295)
    mode: Literal["strict", "study"] | None = None


class ExamAnswerIn(BaseModel):
    content: str = Field(default="", max_length=20_000)


class ExamWorkspaceCodePut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str = Field(default="", max_length=200_000)
    language: str = Field(default="python", max_length=32)
    version: int = Field(default=0, ge=0)


class ExamWorkspaceBoardPut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    snapshot: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)


class ExamCodeRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0)


class ExamStudyAiIn(BaseModel):
    """A user question for the server-owned Study AI exam context.

    The browser may select only the existing routing preferences.  It cannot
    supply context, grading data, or a prompt envelope; those are built from
    the immutable exam workspace on the server.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=100_000)
    workspace_version: int = Field(ge=0)
    model: Literal["auto", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"] = "auto"
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    tutor_mode: Literal["hint", "hint_ladder", "feynman", "socratic", "debug", "review"] = "hint"
