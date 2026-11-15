from release_agent.models import Requirement, RequirementDecision
from release_agent.report import overall_decision


def _d(decision, mandatory=True):
    return RequirementDecision(Requirement("YNA-1", "REQ-01", "x", "c", mandatory), decision, "e")


def test_any_fail_blocks():
    assert overall_decision([_d("PASS"), _d("INSUFFICIENT_EVIDENCE"), _d("FAIL")]) == "BLOCKED"


def test_insufficient_needs_review():
    assert overall_decision([_d("PASS"), _d("INSUFFICIENT_EVIDENCE")]) == "NEEDS_REVIEW"


def test_all_pass_ready():
    assert overall_decision([_d("PASS"), _d("PASS")]) == "READY"


def test_optional_failures_do_not_block():
    assert overall_decision([_d("PASS"), _d("FAIL", mandatory=False)]) == "READY"


def test_no_requirements_needs_review():
    assert overall_decision([]) == "NEEDS_REVIEW"
