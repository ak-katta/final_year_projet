"""
state.py — Central state definition for the LLM QA Tester.

Design goals:
  • Single source of truth across all phases (init → report)
  • Fully serializable (JSON) for save/resume/audit
  • Pydantic v2 for validation, especially of LLM output
  • Bounded lists so long runs don't blow up memory
  • Runtime-only fields (browser/page handles) excluded from serialization
  • Rich screenshot model for a webapp UI (galleries, storyboards, diffs)
  • Schema-versioned with migrations for safe evolution

Schema history:
  v1 → v2  : screenshot list[str] replaced by list[Screenshot] + indexes
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)


# ═══════════════════════════════════════════════════════════════
# SCHEMA VERSION
# ═══════════════════════════════════════════════════════════════

CURRENT_SCHEMA_VERSION = 2


# ═══════════════════════════════════════════════════════════════
# ENUMS — shared vocabulary, prevents string typo bugs
# ═══════════════════════════════════════════════════════════════

class Phase(str, Enum):
    INIT       = "init"
    NAVIGATED  = "navigated"
    ANALYZED   = "analyzed"
    PLANNED    = "planned"
    EXECUTING  = "executing"
    REPORTING  = "reporting"
    DONE       = "done"
    FAILED     = "failed"
    ABORTED    = "aborted"


class SiteType(str, Enum):
    ECOMMERCE   = "ecommerce"
    BLOG        = "blog"
    SAAS        = "saas_dashboard"
    SOCIAL      = "social"
    FORM        = "form"
    SEARCH      = "search"
    PORTFOLIO   = "portfolio"
    DOCS        = "docs"
    OTHER       = "other"


class TestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED  = "passed"
    FAILED  = "failed"
    SKIPPED = "skipped"
    FLAKY   = "flaky"
    ERROR   = "error"       # infra error, not app bug


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH     = "high"
    MEDIUM   = "medium"
    LOW      = "low"
    INFO     = "info"


class TestCategory(str, Enum):
    FUNCTIONAL    = "functional"
    UI            = "ui"
    ACCESSIBILITY = "a11y"
    PERFORMANCE   = "performance"
    SECURITY      = "security"
    COMPATIBILITY = "compatibility"
    VISUAL        = "visual"
    CONTENT       = "content"
    SEO           = "seo"


class ActionType(str, Enum):
    CLICK          = "click"
    FILL           = "fill"
    SELECT         = "select"
    GOTO           = "goto"
    PRESS          = "press"
    HOVER          = "hover"
    WAIT           = "wait"
    ASSERT_TEXT    = "assert_text"
    ASSERT_URL     = "assert_url"
    ASSERT_VISIBLE = "assert_visible"
    SCROLL         = "scroll"
    SCREENSHOT     = "screenshot"


# Screenshot kinds — used by UI to categorize / filter
ScreenshotKind = Literal[
    "viewport",         # default visible area
    "full_page",        # entire scrollable page
    "element",          # a single selector
    "mobile",           # mobile viewport
    "tablet",           # tablet viewport
    "dark_mode",        # forced dark
    "baseline",         # visual regression reference
    "diff",             # visual regression diff image
    "before_action",    # before a step
    "after_action",     # after a step
    "on_failure",       # auto-captured when test fails
    "phase_change",     # captured at phase boundaries
    "thumbnail",        # small preview for UI lists
]


# ═══════════════════════════════════════════════════════════════
# SUB-STRUCTURES
# ═══════════════════════════════════════════════════════════════

class InteractiveElement(BaseModel):
    """A clickable / fillable thing on the page. LLM plans from these."""
    model_config = ConfigDict(extra="ignore")

    tag: str
    text: str = ""
    selector: str = ""
    href: str = ""
    input_type: str = ""
    name: str = ""
    placeholder: str = ""
    is_visible: bool = True
    is_enabled: bool = True
    bounding_box: dict[str, float] = Field(default_factory=dict)
    aria_label: str = ""


class FormField(BaseModel):
    name: str = ""
    type: str = "text"
    required: bool = False
    placeholder: str = ""
    selector: str = ""


class FormInfo(BaseModel):
    action: str = ""
    method: str = "GET"
    fields: list[FormField] = Field(default_factory=list)
    submit_selector: str = ""


class LinkInfo(BaseModel):
    href: str = ""
    text: str = ""
    external: bool = False
    is_broken: Optional[bool] = None


class ImageInfo(BaseModel):
    src: str = ""
    alt: str = ""
    loaded_ok: bool = True
    natural_width: int = 0
    natural_height: int = 0


class PageSnapshot(BaseModel):
    """Everything captured from one page visit."""
    model_config = ConfigDict(extra="ignore")

    url: str = ""
    title: str = ""
    dom_excerpt: str = ""
    dom_hash: str = ""
    text_content: str = ""
    interactive_elements: list[InteractiveElement] = Field(default_factory=list)
    forms: list[FormInfo] = Field(default_factory=list)
    links: list[LinkInfo] = Field(default_factory=list)
    images: list[ImageInfo] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    load_time_ms: float = 0.0
    captured_at: datetime = Field(default_factory=datetime.now)


class Screenshot(BaseModel):
    """
    One captured image with full context for the webapp UI.
    Kept small (~500 bytes JSON) — actual file lives on disk.
    """
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: f"SHOT-{uuid4().hex[:8]}")

    # Storage
    path: str = ""                        # absolute or relative to cwd
    relative_path: str = ""               # relative to output_dir, for webapp
    thumbnail_path: Optional[str] = None  # small preview
    url: str = ""                         # page URL when captured

    # Classification
    kind: ScreenshotKind = "viewport"
    label: str = ""
    description: str = ""

    # Flow context
    phase: str = ""
    test_id: Optional[str] = None
    test_name: str = ""
    step_index: Optional[int] = None
    step_description: str = ""

    # Region (element capture)
    selector: Optional[str] = None
    bounding_box: Optional[dict] = None

    # Image metadata
    width: int = 0
    height: int = 0
    file_size_bytes: int = 0
    format: Literal["png", "jpeg", "webp"] = "png"

    # Visual regression
    baseline_for: Optional[str] = None    # id of baseline this compares against
    diff_pct: Optional[float] = None
    diff_passed: Optional[bool] = None

    # Evidence linkage
    bug_id: Optional[str] = None

    # Meta
    viewport: dict = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=datetime.now)
    tags: list[str] = Field(default_factory=list)


class Step(BaseModel):
    """One atomic action inside a test case."""
    model_config = ConfigDict(extra="ignore")

    index: int = 0
    description: str = ""
    action: ActionType = ActionType.CLICK
    selector: str = ""
    value: str = ""
    timeout_ms: int = Field(default=5000, ge=100, le=60_000)
    optional: bool = False
    status: TestStatus = TestStatus.PENDING
    error: Optional[str] = None
    duration_ms: float = 0.0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    # Screenshot linkage
    screenshot_before: Optional[str] = None   # Screenshot.id
    screenshot_after: Optional[str] = None    # Screenshot.id

    @model_validator(mode="after")
    def _require_selector_for_interactions(self) -> "Step":
        needs_selector = {
            ActionType.CLICK, ActionType.FILL, ActionType.SELECT,
            ActionType.HOVER, ActionType.ASSERT_VISIBLE,
        }
        if self.action in needs_selector and not self.selector:
            raise ValueError(f"action '{self.action.value}' requires a selector")
        if self.action in (ActionType.FILL, ActionType.SELECT) and not self.value:
            raise ValueError(f"action '{self.action.value}' requires a value")
        return self


class TestCase(BaseModel):
    """One test the LLM planned."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: f"T-{uuid4().hex[:6]}")
    name: str
    category: TestCategory = TestCategory.FUNCTIONAL
    description: str = ""
    steps: list[Step] = Field(min_length=1)
    expected: str = ""
    severity_if_fail: Severity = Severity.MEDIUM
    tags: list[str] = Field(default_factory=list)

    status: TestStatus = TestStatus.PENDING
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_ms: float = 0.0
    retries: int = 0

    # Artifacts
    screenshot_ids: list[str] = Field(default_factory=list)
    trace_path: Optional[str] = None
    video_path: Optional[str] = None


class Bug(BaseModel):
    """A confirmed failure worth reporting."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: f"BUG-{uuid4().hex[:6]}")
    test_id: str = ""
    title: str
    description: str = ""
    severity: Severity = Severity.MEDIUM
    category: TestCategory = TestCategory.FUNCTIONAL
    steps_to_reproduce: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    url: str = ""

    # Evidence
    screenshot_id: Optional[str] = None           # hero / primary
    screenshot_ids: list[str] = Field(default_factory=list)   # all evidence
    video_timestamp_ms: Optional[int] = None

    # Signals
    console_errors: list[str] = Field(default_factory=list)
    network_failures: list[str] = Field(default_factory=list)

    found_at: datetime = Field(default_factory=datetime.now)
    fingerprint: str = ""                          # dedupe across runs


class LLMCall(BaseModel):
    """Audit log for every LLM request."""
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    purpose: str                    # classify | plan | step_convert | judge | report | repair
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    ok: bool = True
    error: Optional[str] = None
    attempt: int = 1
    timestamp: datetime = Field(default_factory=datetime.now)


class A11yIssue(BaseModel):
    rule: str = ""
    impact: Literal["critical", "serious", "moderate", "minor", ""] = ""
    selector: str = ""
    description: str = ""
    help_url: str = ""


class NetworkEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    method: str = "GET"
    status: int = 0
    ok: bool = True
    resource_type: str = ""
    duration_ms: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.now)


class VisualDiff(BaseModel):
    baseline_screenshot_id: str = ""
    current_screenshot_id: str = ""
    diff_screenshot_id: str = ""
    diff_pct: float = 0.0
    threshold_pct: float = 0.1
    passed: bool = True
    url: str = ""


class ConsoleLog(BaseModel):
    type: str = "log"               # log | warning | error | info | debug
    text: str = ""
    location: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)


# ═══════════════════════════════════════════════════════════════
# MAIN STATE
# ═══════════════════════════════════════════════════════════════

class QAState(BaseModel):
    """
    The single source of truth for one QA run.

    Lifecycle:
        INIT ─► NAVIGATED ─► ANALYZED ─► PLANNED ─► EXECUTING ─► REPORTING ─► DONE
                                                   │
                                                   └─► FAILED / ABORTED (any phase)
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,   # Playwright handles
        validate_assignment=True,        # catch bad writes
        use_enum_values=False,           # keep Enum instances in Python
        extra="forbid",                  # reject typos
    )

    # ── Schema ────────────────────────────────────────────────
    schema_version: int = CURRENT_SCHEMA_VERSION

    # ── Identity ──────────────────────────────────────────────
    run_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    phase: Phase = Phase.INIT
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    duration_ms: float = 0.0

    # ── Target ────────────────────────────────────────────────
    url: str
    site_type: SiteType = SiteType.OTHER
    site_type_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    site_metadata: dict[str, Any] = Field(default_factory=dict)
    viewport: dict[str, int] = Field(
        default_factory=lambda: {"width": 1440, "height": 900}
    )
    user_agent: str = ""

    # ── Page Data ─────────────────────────────────────────────
    initial_snapshot: Optional[PageSnapshot] = None
    page_snapshots: list[PageSnapshot] = Field(default_factory=list)
    max_snapshots: int = 20

    # ── Planning ──────────────────────────────────────────────
    test_plan: list[TestCase] = Field(default_factory=list)
    plan_generated_by: str = ""
    plan_revision_count: int = 0
    plan_notes: str = ""

    # ── Execution ─────────────────────────────────────────────
    current_test_id: Optional[str] = None
    current_step_index: int = 0
    completed_test_ids: list[str] = Field(default_factory=list)
    failed_test_ids: list[str] = Field(default_factory=list)
    skipped_test_ids: list[str] = Field(default_factory=list)
    execution_started_at: Optional[datetime] = None
    execution_finished_at: Optional[datetime] = None
    max_test_retries: int = 1

    # ── Screenshots (rich) ────────────────────────────────────
    screenshots: list[Screenshot] = Field(default_factory=list)
    screenshots_by_test: dict[str, list[str]] = Field(default_factory=dict)
    screenshots_by_bug: dict[str, list[str]] = Field(default_factory=dict)
    baseline_screenshots: dict[str, str] = Field(default_factory=dict)  # page_key → shot_id
    max_screenshots: int = 200

    screenshot_config: dict[str, Any] = Field(default_factory=lambda: {
        "on_every_step": False,
        "on_failure": True,
        "on_phase_change": True,
        "full_page_on_error": True,
        "generate_thumbnails": True,
        "thumbnail_size": [320, 200],
        "format": "png",
        "quality": 80,
        "mobile_viewports": [[375, 812], [414, 896]],
        "dark_mode": False,
    })

    # ── Video / Trace ─────────────────────────────────────────
    video_path: Optional[str] = None
    trace_path: Optional[str] = None
    output_dir: str = "runs"

    # ── Diagnostics ───────────────────────────────────────────
    console_logs: list[ConsoleLog] = Field(default_factory=list)
    console_errors: list[str] = Field(default_factory=list)
    page_errors: list[str] = Field(default_factory=list)
    network_events: list[NetworkEvent] = Field(default_factory=list)
    network_failures: list[NetworkEvent] = Field(default_factory=list)
    request_failures: list[str] = Field(default_factory=list)
    max_logs: int = 500

    # ── Accessibility / Visual ────────────────────────────────
    a11y_issues: list[A11yIssue] = Field(default_factory=list)
    visual_diffs: list[VisualDiff] = Field(default_factory=list)

    # ── Performance ───────────────────────────────────────────
    page_load_ms: float = 0.0
    ttfb_ms: float = 0.0
    dom_content_loaded_ms: float = 0.0
    lighthouse_scores: dict[str, float] = Field(default_factory=dict)

    # ── LLM Bookkeeping ───────────────────────────────────────
    llm_calls: list[LLMCall] = Field(default_factory=list)
    current_provider: str = ""
    current_model: str = ""
    provider_cooldowns: dict[str, float] = Field(default_factory=dict)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_llm_calls: int = 0
    failed_llm_calls: int = 0
    estimated_cost_usd: float = 0.0
    max_llm_calls: int = 500

    # ── Findings ──────────────────────────────────────────────
    bugs: list[Bug] = Field(default_factory=list)
    severity_counts: dict[str, int] = Field(default_factory=dict)
    category_counts: dict[str, int] = Field(default_factory=dict)
    pass_rate: float = 0.0

    # ── Report ────────────────────────────────────────────────
    summary: str = ""
    full_report: str = ""
    report_path: Optional[str] = None
    report_format: Literal["markdown", "html", "json"] = "markdown"

    # ── Errors / Resilience ───────────────────────────────────
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    retries: int = 0
    fatal_error: Optional[str] = None

    # ── Runtime-only (never serialized) ───────────────────────
    _browser: Any = PrivateAttr(default=None)
    _page: Any = PrivateAttr(default=None)
    _playwright: Any = PrivateAttr(default=None)

    # ═══════════════════════════════════════════════════════════
    # VALIDATORS
    # ═══════════════════════════════════════════════════════════

    @field_validator("url")
    @classmethod
    def _url_has_scheme(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v

    @field_validator("run_id")
    @classmethod
    def _run_id_safe(cls, v: str) -> str:
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("run_id must be alphanumeric (dashes/underscores allowed)")
        return v

    @model_validator(mode="after")
    def _phase_consistency(self) -> "QAState":
        if self.phase == Phase.DONE and self.finished_at is None:
            self.finished_at = datetime.now()
        return self

    # ═══════════════════════════════════════════════════════════
    # HELPER METHODS — the boring but essential plumbing
    # ═══════════════════════════════════════════════════════════

    # ── Phase management ──────────────────────────────────────

    def mark_phase(self, phase: Phase) -> None:
        self.phase = phase

    def mark_failed(self, reason: str) -> None:
        self.fatal_error = reason
        self.add_error(self.phase.value, reason)
        self.phase = Phase.FAILED

    def mark_aborted(self, reason: str) -> None:
        self.fatal_error = reason
        self.add_error(self.phase.value, f"aborted: {reason}")
        self.phase = Phase.ABORTED

    # ── Snapshots ─────────────────────────────────────────────

    def add_snapshot(self, snap: PageSnapshot) -> None:
        self.page_snapshots.append(snap)
        if len(self.page_snapshots) > self.max_snapshots:
            self.page_snapshots.pop(0)

    # ── Screenshots ───────────────────────────────────────────

    def add_screenshot(self, shot: Screenshot) -> None:
        """Register a screenshot + maintain indexes."""
        self.screenshots.append(shot)

        if shot.test_id:
            self.screenshots_by_test.setdefault(shot.test_id, []).append(shot.id)
        if shot.bug_id:
            self.screenshots_by_bug.setdefault(shot.bug_id, []).append(shot.id)

        if len(self.screenshots) > self.max_screenshots:
            dropped = self.screenshots.pop(0)
            self._remove_from_indexes(dropped.id)

    def _remove_from_indexes(self, shot_id: str) -> None:
        for idx in (self.screenshots_by_test, self.screenshots_by_bug):
            for key in list(idx.keys()):
                if shot_id in idx[key]:
                    idx[key].remove(shot_id)
                    if not idx[key]:
                        del idx[key]

    def get_screenshot(self, shot_id: str) -> Optional[Screenshot]:
        return next((s for s in self.screenshots if s.id == shot_id), None)

    def get_screenshots_for_test(self, test_id: str) -> list[Screenshot]:
        return [
            s for s in self.screenshots
            if s.test_id == test_id
        ]

    def get_screenshots_for_bug(self, bug_id: str) -> list[Screenshot]:
        return [s for s in self.screenshots if s.bug_id == bug_id]

    def get_primary_evidence(self, bug_id: str) -> Optional[Screenshot]:
        """First on-failure shot for a bug, else first available."""
        shots = self.get_screenshots_for_bug(bug_id)
        for s in shots:
            if s.kind == "on_failure":
                return s
        return shots[0] if shots else None

    def screenshots_by_kind(self, kind: str) -> list[Screenshot]:
        return [s for s in self.screenshots if s.kind == kind]

    def register_baseline(self, page_key: str, shot_id: str) -> None:
        self.baseline_screenshots[page_key] = shot_id

    def get_baseline(self, page_key: str) -> Optional[Screenshot]:
        sid = self.baseline_screenshots.get(page_key)
        return self.get_screenshot(sid) if sid else None

    # ── Artifacts ─────────────────────────────────────────────

    def add_video(self, path: str) -> None:
        self.video_path = path

    def add_trace(self, path: str) -> None:
        self.trace_path = path

    # ── Diagnostics ───────────────────────────────────────────

    def add_console(self, entry: ConsoleLog | dict) -> None:
        if isinstance(entry, dict):
            entry = ConsoleLog(**entry)
        self.console_logs.append(entry)
        if entry.type == "error" and entry.text not in self.console_errors:
            self.console_errors.append(entry.text)
        if len(self.console_logs) > self.max_logs:
            self.console_logs.pop(0)

    def add_page_error(self, msg: str) -> None:
        if msg and msg not in self.page_errors:
            self.page_errors.append(msg)

    def add_network_event(self, ev: NetworkEvent) -> None:
        self.network_events.append(ev)
        if not ev.ok or ev.status >= 400:
            self.network_failures.append(ev)
        if len(self.network_events) > self.max_logs:
            self.network_events.pop(0)

    def add_request_failure(self, url: str) -> None:
        if url not in self.request_failures:
            self.request_failures.append(url)

    # ── LLM bookkeeping ───────────────────────────────────────

    def add_llm_call(self, call: LLMCall) -> None:
        self.llm_calls.append(call)
        self.total_llm_calls += 1
        if call.ok:
            self.current_provider = call.provider
            self.current_model = call.model
            self.total_prompt_tokens += call.prompt_tokens
            self.total_completion_tokens += call.completion_tokens
        else:
            self.failed_llm_calls += 1
        if len(self.llm_calls) > self.max_llm_calls:
            self.llm_calls.pop(0)

    def set_provider_cooldown(self, provider: str, seconds: float) -> None:
        self.provider_cooldowns[provider] = time.time() + seconds

    def provider_available(self, provider: str) -> bool:
        return self.provider_cooldowns.get(provider, 0.0) < time.time()

    # ── Errors ────────────────────────────────────────────────

    def add_error(self, phase: str, msg: str) -> None:
        self.errors.append({
            "phase": phase,
            "msg": msg,
            "ts": datetime.now().isoformat(),
        })

    def add_warning(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    # ── Test plan helpers ─────────────────────────────────────

    def get_test(self, test_id: str) -> Optional[TestCase]:
        return next((t for t in self.test_plan if t.id == test_id), None)

    def get_current_test(self) -> Optional[TestCase]:
        if not self.current_test_id:
            return None
        return self.get_test(self.current_test_id)

    def get_current_step(self) -> Optional[Step]:
        test = self.get_current_test()
        if not test:
            return None
        if 0 <= self.current_step_index < len(test.steps):
            return test.steps[self.current_step_index]
        return None

    def start_test(self, test_id: str) -> None:
        test = self.get_test(test_id)
        if not test:
            raise KeyError(f"unknown test id: {test_id}")
        self.current_test_id = test_id
        self.current_step_index = 0
        test.status = TestStatus.RUNNING
        test.started_at = datetime.now()

    def finish_test(
        self,
        test_id: str,
        status: TestStatus,
        error: Optional[str] = None,
    ) -> None:
        test = self.get_test(test_id)
        if not test:
            return
        test.status = status
        test.finished_at = datetime.now()
        if test.started_at:
            test.duration_ms = (
                test.finished_at - test.started_at
            ).total_seconds() * 1000
        if error:
            test.error = error

        if status == TestStatus.PASSED:
            if test_id not in self.completed_test_ids:
                self.completed_test_ids.append(test_id)
        elif status in (TestStatus.FAILED, TestStatus.ERROR):
            if test_id not in self.failed_test_ids:
                self.failed_test_ids.append(test_id)
        elif status == TestStatus.SKIPPED:
            if test_id not in self.skipped_test_ids:
                self.skipped_test_ids.append(test_id)

        self.current_test_id = None
        self.current_step_index = 0

    # ── Bugs ──────────────────────────────────────────────────

    def add_bug(self, bug: Bug) -> None:
        """Add a bug, deduping by fingerprint when present."""
        if bug.fingerprint:
            for existing in self.bugs:
                if existing.fingerprint == bug.fingerprint:
                    return
        self.bugs.append(bug)

    # ── Finalization ──────────────────────────────────────────

    def finish(self) -> None:
        self.finished_at = datetime.now()
        self.duration_ms = (
            self.finished_at - self.started_at
        ).total_seconds() * 1000

        self.severity_counts = {}
        for b in self.bugs:
            key = b.severity.value
            self.severity_counts[key] = self.severity_counts.get(key, 0) + 1

        self.category_counts = {}
        for t in self.test_plan:
            key = t.category.value
            self.category_counts[key] = self.category_counts.get(key, 0) + 1

        total = len(self.test_plan)
        self.pass_rate = (
            len(self.completed_test_ids) / total if total else 0.0
        )

        self.phase = Phase.DONE

    # ── Diagnostics summary ───────────────────────────────────

    def health_snapshot(self) -> dict[str, Any]:
        """Compact view for logging / dashboards."""
        return {
            "run_id": self.run_id,
            "phase": self.phase.value,
            "url": self.url,
            "site_type": self.site_type.value,
            "tests_planned": len(self.test_plan),
            "tests_passed": len(self.completed_test_ids),
            "tests_failed": len(self.failed_test_ids),
            "bugs": len(self.bugs),
            "screenshots": len(self.screenshots),
            "pass_rate": round(self.pass_rate, 3),
            "llm_calls": self.total_llm_calls,
            "llm_failures": self.failed_llm_calls,
            "tokens": {
                "prompt": self.total_prompt_tokens,
                "completion": self.total_completion_tokens,
            },
            "duration_ms": round(self.duration_ms, 1),
            "fatal_error": self.fatal_error,
        }

    # ── Persistence ───────────────────────────────────────────

    def to_dict(self, include_snapshots: bool = True) -> dict[str, Any]:
        """Serializable dict. Drops browser handles (PrivateAttr)."""
        data = self.model_dump(mode="json")
        if not include_snapshots:
            data.pop("page_snapshots", None)
            data.pop("initial_snapshot", None)
        return data

    def to_json(self, *, include_snapshots: bool = True, indent: int = 2) -> str:
        data = self.to_dict(include_snapshots=include_snapshots)
        return json.dumps(data, indent=indent, default=str)

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(indent=2), encoding="utf-8")
        return p

    def save_compact(self, path: str | Path) -> Path:
        """Save without bulky snapshots — for long-term storage."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            self.to_json(include_snapshots=False, indent=None),
            encoding="utf-8",
        )
        return p

    @classmethod
    def load(cls, path: str | Path) -> "QAState":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        data = cls.migrate(data)
        cls.model_config["extra"] = "ignore"
        try:
            return cls.model_validate(data)
        finally:
            cls.model_config["extra"] = "forbid"

    # ── Schema migration ──────────────────────────────────────

    @classmethod
    def migrate(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Bring an old state dict up to CURRENT_SCHEMA_VERSION."""
        version = data.get("schema_version", 0)

        # ── v0 → v1 : add phase/duration, rename tests → test_plan ──
        if version < 1:
            if "tests" in data and "test_plan" not in data:
                data["test_plan"] = data.pop("tests")
            data.setdefault("phase", "init")
            data.setdefault("duration_ms", 0.0)
            version = 1

        # ── v1 → v2 : screenshots list[str] → list[Screenshot] + indexes ──
        if version < 2:
            old = data.get("screenshots", [])
            if old and isinstance(old[0], str):
                data["screenshots"] = [
                    {
                        "id": f"SHOT-{i:08x}",
                        "path": p,
                        "relative_path": p,
                        "kind": "viewport",
                        "label": f"migrated-{i}",
                        "phase": data.get("phase", "init"),
                        "captured_at": data.get("started_at"),
                    }
                    for i, p in enumerate(old)
                ]
            data.setdefault("screenshots_by_test", {})
            data.setdefault("screenshots_by_bug", {})
            data.setdefault("baseline_screenshots", {})
            data.setdefault("screenshot_config", {
                "on_every_step": False,
                "on_failure": True,
                "on_phase_change": True,
                "full_page_on_error": True,
                "generate_thumbnails": True,
                "thumbnail_size": [320, 200],
                "format": "png",
                "quality": 80,
                "mobile_viewports": [[375, 812], [414, 896]],
                "dark_mode": False,
            })

            # Step: add screenshot_before/after fields
            for test in data.get("test_plan", []):
                for step in test.get("steps", []):
                    step.setdefault("screenshot_before", None)