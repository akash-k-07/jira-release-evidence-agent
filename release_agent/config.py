import os
from dataclasses import dataclass, field, fields

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    jira_base_url: str
    jira_email: str
    jira_api_token: str = field(repr=False)
    jira_project_key: str
    dataset_label: str
    target_release: str
    jira_issue_type: str
    jira_link_type: str
    llm_provider: str
    llm_model: str
    llm_api_key: str = field(repr=False)
    llm_base_url: str
    llm_temperature: float
    llm_max_tokens: int
    max_discovery_steps: int
    max_requirement_steps: int
    max_tool_calls: int
    max_runtime_seconds: int
    tool_result_max_chars: int
    max_parallel: int

    def require(self, *names: str) -> None:
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            env_names = ", ".join(name.upper() for name in missing)
            raise SystemExit(f"Missing configuration: {env_names}. Set them in .env or pass them as arguments.")


def load_settings(**overrides) -> Settings:
    load_dotenv()
    values = {
        "jira_base_url": os.getenv("JIRA_BASE_URL", "").rstrip("/"),
        "jira_email": os.getenv("JIRA_EMAIL", ""),
        "jira_api_token": os.getenv("JIRA_API_TOKEN", ""),
        "jira_project_key": os.getenv("JIRA_PROJECT_KEY", ""),
        "dataset_label": os.getenv("DATASET_LABEL", "tyn-release-assessment-v2"),
        "target_release": os.getenv("TARGET_RELEASE", "R2.4"),
        "jira_issue_type": os.getenv("JIRA_ISSUE_TYPE", "Task"),
        "jira_link_type": os.getenv("JIRA_LINK_TYPE", "Relates"),
        "llm_provider": os.getenv("LLM_PROVIDER", "placeholder"),
        "llm_model": os.getenv("LLM_MODEL", ""),
        "llm_api_key": os.getenv("LLM_API_KEY", ""),
        "llm_base_url": os.getenv("LLM_BASE_URL", ""),
        "llm_temperature": float(os.getenv("LLM_TEMPERATURE", "0")),
        "llm_max_tokens": int(os.getenv("LLM_MAX_TOKENS", "2048")),
        "max_discovery_steps": int(os.getenv("AGENT_MAX_DISCOVERY_STEPS", "12")),
        "max_requirement_steps": int(os.getenv("AGENT_MAX_REQUIREMENT_STEPS", "16")),
        "max_tool_calls": int(os.getenv("AGENT_MAX_TOOL_CALLS", "400")),
        "max_runtime_seconds": int(os.getenv("AGENT_MAX_RUNTIME_SECONDS", "1800")),
        "tool_result_max_chars": int(os.getenv("TOOL_RESULT_MAX_CHARS", "8000")),
        "max_parallel": int(os.getenv("AGENT_MAX_PARALLEL", "4")),
    }
    known = {f.name for f in fields(Settings)}
    values.update({k: v for k, v in overrides.items() if k in known and v is not None})
    if values["jira_project_key"]:
        values["jira_project_key"] = values["jira_project_key"].upper()
    return Settings(**values)
