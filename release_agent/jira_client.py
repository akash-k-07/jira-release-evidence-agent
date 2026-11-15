import time
from typing import Any, Iterator

import httpx

IDEMPOTENT_METHODS = {"GET", "HEAD"}


class JiraError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if not isinstance(body, dict):
        return str(body)[:300]
    messages = list(body.get("errorMessages") or [])
    messages += [f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
    return "; ".join(messages) or resp.text[:300]


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: float = 30.0, max_retries: int = 5):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.request_count = 0
        self._http = httpx.Client(
            base_url=f"{self.base_url}/rest/api/3",
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, path: str, idempotent: bool | None = None, **kwargs) -> Any:
        retry_server_errors = method in IDEMPOTENT_METHODS if idempotent is None else idempotent
        status, detail = 0, ""
        for attempt in range(self.max_retries + 1):
            self.request_count += 1
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                status, detail = 0, str(exc)
                if not retry_server_errors or attempt == self.max_retries:
                    break
                time.sleep(min(2**attempt, 30))
                continue
            retryable = resp.status_code == 429 or (resp.status_code >= 500 and retry_server_errors)
            if retryable and attempt < self.max_retries:
                time.sleep(self._retry_delay(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise JiraError(resp.status_code, f"{method} {path} -> {resp.status_code}: {_error_text(resp)}")
            return resp.json() if resp.content else None
        raise JiraError(status, f"{method} {path} failed after retries: {detail}")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After", "")
        return float(header) if header.isdigit() else float(min(2**attempt, 30))

    def myself(self) -> dict:
        return self._request("GET", "/myself")

    def list_projects(self) -> list[dict]:
        return self._request("GET", "/project/search", params={"maxResults": 50}).get("values", [])

    def get_project(self, key: str) -> dict:
        return self._request("GET", f"/project/{key}")

    def link_types(self) -> list[dict]:
        return self._request("GET", "/issueLinkType").get("issueLinkTypes", [])

    def search(self, jql: str, fields: list[str], page_size: int = 100) -> Iterator[dict]:
        token = None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": page_size}
            if token:
                body["nextPageToken"] = token
            page = self._request("POST", "/search/jql", idempotent=True, json=body)
            yield from page.get("issues", [])
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return

    def get_issue(self, key: str, fields: list[str]) -> dict:
        return self._request("GET", f"/issue/{key}", params={"fields": ",".join(fields)})

    def comment_total(self, key: str) -> int:
        return self._request("GET", f"/issue/{key}/comment", params={"maxResults": 0}).get("total", 0)

    def get_all_comments(self, key: str) -> list[dict]:
        comments: list[dict] = []
        while True:
            page = self._request(
                "GET", f"/issue/{key}/comment", params={"startAt": len(comments), "maxResults": 100, "orderBy": "created"}
            )
            batch = page.get("comments", [])
            comments.extend(batch)
            if not batch or len(comments) >= page.get("total", 0):
                return comments

    def bulk_create(self, issue_fields: list[dict]) -> dict:
        return self._request("POST", "/issue/bulk", json={"issueUpdates": [{"fields": f} for f in issue_fields]})

    def add_comment(self, key: str, body_adf: dict) -> dict:
        return self._request("POST", f"/issue/{key}/comment", json={"body": body_adf})

    def create_link(self, link_type: str, inward_key: str, outward_key: str) -> None:
        self._request(
            "POST",
            "/issueLink",
            json={"type": {"name": link_type}, "inwardIssue": {"key": inward_key}, "outwardIssue": {"key": outward_key}},
        )
