import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .jira_client import JiraError
from .textutil import text_to_adf

log = logging.getLogger(__name__)

SUMMARY_LIMIT = 255
REQUIRED_RECORD_KEYS = ("source_id", "kind", "summary", "description", "comments", "links")


@dataclass
class ImportSummary:
    records: int = 0
    issues_created: int = 0
    issues_reused: int = 0
    comments_expected: int = 0
    comments_posted: int = 0
    comments_already_present: int = 0
    links_expected: int = 0
    links_created: int = 0
    links_already_present: int = 0
    missing_link_targets: list[str] = field(default_factory=list)


def load_dataset(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("dataset_label") or not isinstance(data.get("records"), list):
        raise ValueError("dataset must contain 'dataset_label' and a 'records' list")
    seen: set[str] = set()
    for index, record in enumerate(data["records"]):
        missing = [k for k in REQUIRED_RECORD_KEYS if k not in record]
        if missing:
            raise ValueError(f"record {index} is missing {missing}")
        if record["source_id"] in seen:
            raise ValueError(f"duplicate source_id {record['source_id']}")
        seen.add(record["source_id"])
    return data


def _label(value: str) -> str:
    return "".join("_" if ch.isspace() else ch for ch in value)


def record_labels(record: dict, dataset_label: str) -> list[str]:
    labels = [dataset_label, f"src-{record['source_id']}", f"kind-{record['kind']}"]
    if record.get("release"):
        labels.append(f"release-{record['release']}")
    if record["kind"] == "requirement":
        labels.append("rr-requirement")
    return [_label(label) for label in labels]


def record_summary(record: dict) -> str:
    return f"[{record['source_id']}] {record['summary']}"[:SUMMARY_LIMIT]


def record_description(record: dict) -> str:
    header = [
        f"Source ID: {record['source_id']}",
        f"Record kind: {record['kind']}",
        f"Recorded release: {record.get('release') or 'none'}",
        f"Recorded status: {record.get('recorded_status') or 'none'}",
        f"Recorded links: {', '.join(record['links']) or 'none'}",
        "",
        "Recorded description:",
    ]
    return "\n".join(header) + "\n" + record["description"]


def build_issue_fields(record: dict, project_key: str, issue_type_id: str, dataset_label: str) -> dict:
    return {
        "project": {"key": project_key},
        "issuetype": {"id": issue_type_id},
        "summary": record_summary(record),
        "description": text_to_adf(record_description(record)),
        "labels": record_labels(record, dataset_label),
    }


class Importer:
    def __init__(self, jira, project_key: str, issue_type: str = "Task", link_type: str = "Relates", batch_size: int = 50):
        self.jira = jira
        self.project_key = project_key
        self.issue_type = issue_type
        self.link_type = link_type
        self.batch_size = batch_size

    def run(self, data: dict) -> ImportSummary:
        label = data["dataset_label"]
        records = data["records"]
        summary = ImportSummary(
            records=len(records),
            comments_expected=sum(len(r["comments"]) for r in records),
            links_expected=sum(len(r["links"]) for r in records),
        )
        issue_type_id = self._issue_type_id()
        self._check_link_type()

        keys = self._existing_keys(label)
        summary.issues_reused = sum(1 for r in records if r["source_id"] in keys)
        created = self._create_issues([r for r in records if r["source_id"] not in keys], issue_type_id, label)
        keys.update(created)
        summary.issues_created = len(created)

        self._import_comments(records, keys, set(created), summary)
        self._import_links(records, keys, summary)
        return summary

    def _issue_type_id(self) -> str:
        project = self.jira.get_project(self.project_key)
        for issue_type in project.get("issueTypes", []):
            if issue_type["name"].lower() == self.issue_type.lower() and not issue_type.get("subtask"):
                return issue_type["id"]
        available = ", ".join(t["name"] for t in project.get("issueTypes", []))
        raise SystemExit(f"Issue type '{self.issue_type}' not found in {self.project_key}. Available: {available}")

    def _check_link_type(self) -> None:
        names = [t["name"] for t in self.jira.link_types()]
        if self.link_type not in names:
            raise SystemExit(f"Link type '{self.link_type}' not found. Available: {', '.join(names)}")

    def _existing_keys(self, label: str) -> dict[str, str]:
        keys: dict[str, str] = {}
        jql = f'project = "{self.project_key}" AND labels = "{label}" ORDER BY key ASC'
        for issue in self.jira.search(jql, ["labels"]):
            for value in issue["fields"].get("labels", []):
                if value.startswith("src-"):
                    source_id = value[4:]
                    if source_id in keys:
                        log.warning("Duplicate import of %s: %s and %s", source_id, keys[source_id], issue["key"])
                    keys.setdefault(source_id, issue["key"])
        return keys

    def _create_issues(self, records: list[dict], issue_type_id: str, label: str) -> dict[str, str]:
        created: dict[str, str] = {}
        for start in range(0, len(records), self.batch_size):
            batch = records[start : start + self.batch_size]
            payload = [build_issue_fields(r, self.project_key, issue_type_id, label) for r in batch]
            response = self.jira.bulk_create(payload)
            failed = {e.get("failedElementNumber"): e for e in response.get("errors", [])}
            issues = iter(response.get("issues", []))
            for index, record in enumerate(batch):
                if index in failed:
                    log.error("Failed to create %s: %s", record["source_id"], failed[index].get("elementErrors"))
                    continue
                created[record["source_id"]] = next(issues)["key"]
            if failed:
                raise SystemExit(f"{len(failed)} issues failed to create; fix the cause and re-run (import is resumable)")
            log.info("Created %d/%d issues", len(created), len(records))
        return created

    def _import_comments(self, records: list[dict], keys: dict[str, str], new_keys: set[str], summary: ImportSummary) -> None:
        for position, record in enumerate(records, 1):
            key = keys[record["source_id"]]
            present = 0 if record["source_id"] in new_keys else self.jira.comment_total(key)
            if present > len(record["comments"]):
                log.warning("%s (%s) has %d comments, expected %d", key, record["source_id"], present, len(record["comments"]))
            summary.comments_already_present += min(present, len(record["comments"]))
            for text in record["comments"][present:]:
                self.jira.add_comment(key, text_to_adf(text))
                summary.comments_posted += 1
            if position % 50 == 0:
                log.info("Comments processed for %d/%d issues", position, len(records))

    def _import_links(self, records: list[dict], keys: dict[str, str], summary: ImportSummary) -> None:
        for record in records:
            if not record["links"]:
                continue
            key = keys[record["source_id"]]
            linked = self._linked_keys(key)
            for target_id in record["links"]:
                target_key = keys.get(target_id)
                if not target_key:
                    summary.missing_link_targets.append(f"{record['source_id']}->{target_id}")
                    continue
                if target_key in linked:
                    summary.links_already_present += 1
                    continue
                try:
                    self.jira.create_link(self.link_type, key, target_key)
                except JiraError as exc:
                    log.error("Link %s -> %s failed: %s", record["source_id"], target_id, exc)
                    raise
                linked.add(target_key)
                summary.links_created += 1
        if summary.missing_link_targets:
            log.warning("Links to records not in the dataset: %s", ", ".join(summary.missing_link_targets))

    def _linked_keys(self, key: str) -> set[str]:
        links = self.jira.get_issue(key, ["issuelinks"])["fields"].get("issuelinks", [])
        return {(link.get("outwardIssue") or link.get("inwardIssue"))["key"] for link in links}
