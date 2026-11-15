import re

from release_agent.jira_client import JiraError
from release_agent.textutil import adf_to_text


class FakeJira:
    base_url = "https://fake.atlassian.net"

    def __init__(self, project_key: str = "YNA"):
        self.project_key = project_key
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.links: list[tuple[str, str, str]] = []
        self.request_count = 0
        self._next_issue = 1
        self._next_comment = 10000

    def get_project(self, key):
        return {"key": key, "issueTypes": [{"id": "10001", "name": "Task"}, {"id": "10002", "name": "Subtask", "subtask": True}]}

    def link_types(self):
        return [{"name": "Relates", "inward": "relates to", "outward": "relates to"}]

    def search(self, jql, fields, page_size=100):
        self.request_count += 1
        required = re.findall(r'labels = "([^"]+)"', jql)
        project = re.search(r'project = "([^"]+)"', jql)
        for key in sorted(self.issues, key=lambda k: int(k.split("-")[1])):
            issue = self.issues[key]
            if project and not key.startswith(f"{project[1]}-"):
                continue
            if all(label in issue["labels"] for label in required):
                yield {"key": key, "fields": {f: issue.get(f) for f in fields}}

    def get_issue(self, key, fields):
        self.request_count += 1
        if key not in self.issues:
            raise JiraError(404, f"{key} not found")
        issue = dict(self.issues[key])
        issue["issuelinks"] = self._issuelinks(key)
        return {"key": key, "fields": {f: issue.get(f) for f in fields}}

    def comment_total(self, key):
        return len(self.comments.get(key, []))

    def get_all_comments(self, key):
        self.request_count += 1
        return list(self.comments.get(key, []))

    def bulk_create(self, issue_fields):
        created = []
        for fields in issue_fields:
            key = f"{fields['project']['key']}-{self._next_issue}"
            self._next_issue += 1
            self.issues[key] = {"summary": fields["summary"], "labels": list(fields["labels"]), "description": fields["description"]}
            self.comments[key] = []
            created.append({"id": key, "key": key})
        return {"issues": created, "errors": []}

    def add_comment(self, key, body_adf):
        comment = {"id": str(self._next_comment), "body": body_adf}
        self._next_comment += 1
        self.comments[key].append(comment)
        return comment

    def create_link(self, link_type, inward_key, outward_key):
        self.links.append((link_type, inward_key, outward_key))

    def comment_texts(self, key):
        return [adf_to_text(c["body"]) for c in self.comments[key]]

    def _issuelinks(self, key):
        links = []
        for _, inward, outward in self.links:
            if inward == key:
                links.append({"type": {"name": "Relates", "outward": "relates to"}, "outwardIssue": self._ref(outward)})
            elif outward == key:
                links.append({"type": {"name": "Relates", "inward": "relates to"}, "inwardIssue": self._ref(inward)})
        return links

    def _ref(self, key):
        return {"key": key, "fields": {"summary": self.issues[key]["summary"]}}
