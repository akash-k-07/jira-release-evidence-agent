import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

TARGET = '{"release": "R2.4", "commit": "b7a91f2", "prompt": "p7", "environment": "production", "service": "atlas-assistant"}'


def event(when, actor, role, body):
    return f"Recorded event {when} | actor {actor} | role {role}\n{body}"


@pytest.fixture
def dataset_path():
    return ROOT / "input" / "dataset.json"


@pytest.fixture
def mini_dataset():
    return {
        "dataset_label": "tyn-test",
        "records": [
            {
                "source_id": "MANIFEST",
                "kind": "manifest",
                "summary": "Release manifest for atlas-assistant R2.4",
                "release": "R2.4",
                "recorded_status": "Done",
                "description": f"Target: {TARGET}\nAssess the linked requirement records.",
                "comments": [event("2026-11-08T14:00:00+05:30", "Priya Nair", "QA Lead", "Defines the target.")],
                "links": ["REQ-01"],
            },
            {
                "source_id": "REQ-01",
                "kind": "requirement",
                "summary": "Automated regression tests",
                "release": "R2.4",
                "recorded_status": "Done",
                "description": f"Requirement ID: REQ-01\nTarget: {TARGET}\nMandatory: yes\nAcceptance criterion: All automated regression cases must pass on the target build.",
                "comments": [event("2026-11-07T18:00:00+05:30", "Sam Patel", "Developer", "Implementation complete; marking Done.")],
                "links": ["REC-1", "REC-404"],
            },
            {
                "source_id": "REC-1",
                "kind": "evidence",
                "summary": "Regression run - target build",
                "release": "R2.4",
                "recorded_status": "Done",
                "description": "Engineering record.",
                "comments": [
                    event("2026-11-03T09:00:00+05:30", "Sam Patel", "Developer", "Picked this up."),
                    event("2026-11-08T12:00:00+05:30", "Arjun Mehta", "QA Lead", "REG-804: release R2.4; commit b7a91f2; service atlas-assistant; environment production. 146 of 146 pass."),
                ],
                "links": ["REQ-01"],
            },
        ],
    }
