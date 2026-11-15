import copy
import re
from typing import Callable

from .jira_client import JiraError
from .llm import ToolSpec
from .textutil import adf_to_text, clip, parse_recorded_event, split_summary

ISSUE_FIELDS = ["summary", "labels", "description", "issuelinks"]
ISSUE_KEY = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z")
SOURCE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
ORDER_BY = re.compile(r"\border\s+by\b", re.I)


class ToolError(Exception):
    pass


def _jql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _label_value(labels: list[str], prefix: str) -> str | None:
    return next((label[len(prefix):] for label in labels if label.startswith(prefix)), None)


class JiraEvidenceTools:
    """Read-only Jira tools restricted to one project and dataset label."""

    def __init__(self, jira, project_key: str, dataset_label: str, comment_max_chars: int = 1500, description_max_chars: int = 4000):
        self.jira = jira
        self.project_key = project_key
        self.dataset_label = dataset_label
        self.comment_max_chars = comment_max_chars
        self.description_max_chars = description_max_chars
        self._issues: dict[str, dict] = {}
        self._comments: dict[str, list[dict]] = {}
        self._source_keys: dict[str, str] = {}
        self.reset_session()

    def reset_session(self) -> None:
        self.read_issues: set[str] = set()
        self.listed_issues: set[str] = set()
        self.read_comments: dict[str, set[str]] = {}

    def session(self) -> "JiraEvidenceTools":
        """Independent record of what one investigation has seen, sharing the Jira cache."""
        view = copy.copy(self)
        view.reset_session()
        return view

    @property
    def scope_jql(self) -> str:
        return f'project = "{self.project_key}" AND labels = "{self.dataset_label}"'

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "search_issues",
                "Search imported Jira issues inside the assessment scope (project and dataset label are applied "
                "automatically). Imported issues carry labels src-<source_id>, kind-<kind>, release-<release>, and "
                "rr-requirement for requirement records. Returns brief issue summaries only.",
                {
                    "type": "object",
                    "properties": {
                        "jql_filter": {"type": "string", "description": 'Optional JQL condition ANDed with the scope, e.g. labels = "kind-manifest". No ORDER BY.'},
                        "text": {"type": "string", "description": "Optional full-text search term."},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                    },
                },
            ),
            ToolSpec(
                "get_issue",
                "Read one scoped issue: source ID, summary, labels, recorded description, linked issues and comment count. "
                "Provide issue_key or source_id.",
                {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string", "description": "Jira issue key, e.g. YNA-12."},
                        "source_id": {"type": "string", "description": "Imported source identifier, e.g. REQ-01."},
                    },
                },
            ),
            ToolSpec(
                "get_comments",
                "Read comments on a scoped issue in recorded order. Each comment shows the scenario recorded_at timestamp, "
                "actor and role parsed from its text. Native Jira authors and timestamps are omitted because they belong to "
                "the importer. Use contains to filter by text.",
                {
                    "type": "object",
                    "properties": {
                        "issue_key": {"type": "string"},
                        "start_at": {"type": "integer", "minimum": 0, "default": 0},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                        "contains": {"type": "string", "description": "Optional case-insensitive text filter."},
                    },
                    "required": ["issue_key"],
                },
            ),
        ]

    def handlers(self) -> dict[str, Callable]:
        return {"search_issues": self.search_issues, "get_issue": self.get_issue, "get_comments": self.get_comments}

    def search_issues(self, jql_filter: str | None = None, text: str | None = None, max_results: int = 20) -> dict:
        if jql_filter and ORDER_BY.search(jql_filter):
            raise ToolError("ORDER BY is not allowed in jql_filter")
        clauses = [self.scope_jql]
        if jql_filter:
            clauses.append(f"({jql_filter})")
        if text:
            clauses.append(f'text ~ "{_jql_escape(text)}"')
        limit = max(1, min(int(max_results), 50))
        jql = " AND ".join(clauses) + " ORDER BY key ASC"
        items, more = [], False
        try:
            for issue in self.jira.search(jql, ["summary", "labels"], page_size=min(limit + 1, 100)):
                fields = issue["fields"]
                if not self._in_scope(issue["key"], fields.get("labels") or []):
                    continue
                if len(items) == limit:
                    more = True
                    break
                items.append(self._brief(issue["key"], fields))
        except JiraError as exc:
            raise ToolError(f"search failed: {exc}") from exc
        self.listed_issues.update(item["issue_key"] for item in items)
        return {"jql": jql, "returned": len(items), "more_available": more, "issues": items}

    def get_issue(self, issue_key: str | None = None, source_id: str | None = None) -> dict:
        key = self._resolve(issue_key, source_id)
        record = self.issue_record(key)
        self.read_issues.add(key)
        self.listed_issues.update(link["issue_key"] for link in record["links"])
        return {
            "issue_key": key,
            "source_id": record["source_id"],
            "summary": record["summary"],
            "labels": record["labels"],
            "description": clip(record["description"], self.description_max_chars),
            "links": record["links"],
            "comment_count": len(self._load_comments(key)),
        }

    def get_comments(self, issue_key: str, start_at: int = 0, max_results: int = 20, contains: str | None = None) -> dict:
        key = self._resolve(issue_key, None)
        record = self.issue_record(key)
        comments = self._load_comments(key)
        needle = (contains or "").lower()
        matched = [c for c in comments if needle in c["text"].lower()] if needle else comments
        start = max(0, int(start_at))
        limit = max(1, min(int(max_results), 50))
        page = matched[start : start + limit]
        self.read_issues.add(key)
        self.read_comments.setdefault(key, set()).update(c["comment_id"] for c in page)
        return {
            "issue_key": key,
            "source_id": record["source_id"],
            "total_comments": len(comments),
            "matched": len(matched),
            "start_at": start,
            "returned": len(page),
            "comments": [{**c, "text": clip(c["text"], self.comment_max_chars)} for c in page],
        }

    def issue_record(self, key: str) -> dict:
        if key in self._issues:
            return self._issues[key]
        try:
            issue = self.jira.get_issue(key, ISSUE_FIELDS)
        except JiraError as exc:
            raise ToolError(f"issue {key} could not be read: {exc}") from exc
        fields = issue["fields"]
        labels = fields.get("labels") or []
        if not self._in_scope(issue["key"], labels):
            raise ToolError(f"issue {key} is outside the assessment scope")
        summary_id, summary = split_summary(fields.get("summary", ""))
        record = {
            "issue_key": issue["key"],
            "source_id": _label_value(labels, "src-") or summary_id or "",
            "summary": summary,
            "labels": labels,
            "description": adf_to_text(fields.get("description")),
            "links": [self._link(link) for link in fields.get("issuelinks") or []],
        }
        self._issues[key] = record
        if record["source_id"]:
            self._source_keys[record["source_id"]] = key
        return record

    def comment(self, key: str, comment_id: str) -> dict | None:
        return next((c for c in self._load_comments(key) if c["comment_id"] == comment_id), None)

    def url(self, key: str, comment_id: str | None = None) -> str:
        base = f"{self.jira.base_url}/browse/{key}"
        return f"{base}?focusedCommentId={comment_id}" if comment_id else base

    def _resolve(self, issue_key: str | None, source_id: str | None) -> str:
        if issue_key:
            key = issue_key.strip().upper()
            if not ISSUE_KEY.match(key):
                raise ToolError(f"'{issue_key}' is not a Jira issue key")
            if not key.startswith(f"{self.project_key}-"):
                raise ToolError(f"issue {key} is outside the assessment scope")
            return key
        if not source_id:
            raise ToolError("provide issue_key or source_id")
        source_id = source_id.strip()
        if source_id in self._source_keys:
            return self._source_keys[source_id]
        if not SOURCE_ID.match(source_id):
            raise ToolError(f"invalid source_id '{source_id}'")
        jql = f'{self.scope_jql} AND labels = "src-{source_id}" ORDER BY key ASC'
        try:
            matches = [i["key"] for i in self.jira.search(jql, ["labels"], page_size=5)]
        except JiraError as exc:
            raise ToolError(f"lookup failed: {exc}") from exc
        if not matches:
            raise ToolError(f"no scoped issue has source_id {source_id}")
        self._source_keys[source_id] = matches[0]
        return matches[0]

    def _load_comments(self, key: str) -> list[dict]:
        if key not in self._comments:
            try:
                raw = self.jira.get_all_comments(key)
            except JiraError as exc:
                raise ToolError(f"comments for {key} could not be read: {exc}") from exc
            comments = []
            for index, item in enumerate(raw, 1):
                text = adf_to_text(item.get("body"))
                event = parse_recorded_event(text)
                comments.append(
                    {
                        "comment_id": str(item["id"]),
                        "index": index,
                        "recorded_at": event["recorded_at"] if event else None,
                        "actor": event["actor"] if event else None,
                        "role": event["role"] if event else None,
                        "text": event["body"] if event else text,
                    }
                )
            self._comments[key] = comments
        return self._comments[key]

    def _in_scope(self, key: str, labels: list[str]) -> bool:
        return key.startswith(f"{self.project_key}-") and self.dataset_label in labels

    @staticmethod
    def _brief(key: str, fields: dict) -> dict:
        labels = fields.get("labels") or []
        summary_id, summary = split_summary(fields.get("summary", ""))
        return {
            "issue_key": key,
            "source_id": _label_value(labels, "src-") or summary_id,
            "kind": _label_value(labels, "kind-"),
            "release": _label_value(labels, "release-"),
            "summary": summary,
        }

    @staticmethod
    def _link(link: dict) -> dict:
        outward = "outwardIssue" in link
        other = link["outwardIssue"] if outward else link["inwardIssue"]
        source_id, summary = split_summary(other.get("fields", {}).get("summary", ""))
        link_type = link.get("type", {})
        return {
            "issue_key": other["key"],
            "source_id": source_id,
            "summary": summary,
            "relation": link_type.get("outward" if outward else "inward") or link_type.get("name"),
        }
