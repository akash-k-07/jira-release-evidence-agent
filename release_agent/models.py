from dataclasses import dataclass, field

from .llm import TokenUsage


@dataclass
class Usage:
    llm_calls: int = 0
    llm_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    token_usage_reported: bool = False
    tool_calls: int = 0
    tool_errors: int = 0

    def add_tokens(self, usage: TokenUsage) -> None:
        if usage.input_tokens is not None or usage.output_tokens is not None:
            self.token_usage_reported = True
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0

    def merge(self, other: "Usage") -> None:
        self.llm_calls += other.llm_calls
        self.llm_errors += other.llm_errors
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.token_usage_reported |= other.token_usage_reported
        self.tool_calls += other.tool_calls
        self.tool_errors += other.tool_errors


@dataclass
class Requirement:
    issue_key: str
    source_id: str
    summary: str
    criterion: str
    mandatory: bool = True


@dataclass
class EvidenceRef:
    issue_key: str
    source_id: str
    summary: str
    role: str
    note: str
    url: str
    comment_id: str | None = None
    comment_index: int | None = None
    recorded_at: str | None = None
    actor: str | None = None
    actor_role: str | None = None
    excerpt: str | None = None


@dataclass
class RequirementDecision:
    requirement: Requirement
    decision: str
    explanation: str
    evidence: list[EvidenceRef] = field(default_factory=list)
    outstanding: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    status: str = "ok"
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    duration_s: float = 0.0


@dataclass
class Discovery:
    manifest_key: str
    manifest_source_id: str
    manifest_text: str
    target: dict
    requirements: list[Requirement]


@dataclass
class RunResult:
    generated_at: str
    project_key: str
    dataset_label: str
    target_release: str
    jira_base_url: str
    llm_provider: str
    llm_model: str
    overall_decision: str
    duration_s: float
    usage: Usage
    jira_requests: int
    discovery: Discovery | None
    decisions: list[RequirementDecision]
    errors: list[str] = field(default_factory=list)
