from fake_jira import FakeJira

from release_agent.importer import Importer, build_issue_fields, load_dataset, record_labels
from release_agent.textutil import adf_to_text


def test_real_dataset_loads(dataset_path):
    data = load_dataset(dataset_path)
    assert data["dataset_label"] == "tyn-release-assessment-v2"
    assert len(data["records"]) == 400
    assert sum(len(r["comments"]) for r in data["records"]) == 2059
    assert sum(1 for r in data["records"] if r["kind"] == "requirement") == 12


def test_issue_fields_preserve_source(mini_dataset):
    record = mini_dataset["records"][1]
    fields = build_issue_fields(record, "YNA", "10001", "tyn-test")
    assert fields["summary"] == "[REQ-01] Automated regression tests"
    assert record_labels(record, "tyn-test") == ["tyn-test", "src-REQ-01", "kind-requirement", "release-R2.4", "rr-requirement"]
    description = adf_to_text(fields["description"])
    assert "Recorded status: Done" in description
    assert "Recorded links: REC-1, REC-404" in description
    assert description.endswith(record["description"])


def test_import_is_complete_and_idempotent(mini_dataset):
    jira = FakeJira()
    first = Importer(jira, "YNA").run(mini_dataset)
    assert first.issues_created == 3
    assert first.comments_posted == 4
    assert first.links_created == 2
    assert first.links_already_present == 1
    assert first.missing_link_targets == ["REQ-01->REC-404"]
    assert jira.comment_texts("YNA-3")[1].startswith("Recorded event 2026-11-08T12:00:00+05:30 | actor Arjun Mehta")

    second = Importer(jira, "YNA").run(mini_dataset)
    assert second.issues_created == 0 and second.issues_reused == 3
    assert second.comments_posted == 0 and second.comments_already_present == 4
    assert second.links_created == 0
    assert len(jira.issues) == 3


def test_import_resumes_partial_comments(mini_dataset):
    jira = FakeJira()
    Importer(jira, "YNA").run(mini_dataset)
    jira.comments["YNA-3"].pop()
    summary = Importer(jira, "YNA").run(mini_dataset)
    assert summary.comments_posted == 1
    assert len(jira.comments["YNA-3"]) == 2
