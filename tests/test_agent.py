import json

import pytest
from fake_jira import FakeJira

from release_agent.agent import AgentFailure, ReleaseEvidenceAgent
from release_agent.importer import Importer
from release_agent.llm import LLMClient, LLMResponse, TokenUsage, ToolCall
from release_agent.report import overall_decision, render_markdown
from release_agent.tools import JiraEvidenceTools, ToolError
from release_agent.trace import Trace


class ScriptedLLM(LLMClient):
    provider, model = "scripted", "test"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.offered = []

    def complete(self, system, messages, tools):
        self.calls.append(messages)
        self.offered.append([t.name for t in tools])
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        calls = [ToolCall(f"c{len(self.calls)}-{i}", name, args) for i, (name, args) in enumerate(step)]
        return LLMResponse(text="", tool_calls=calls, usage=TokenUsage(100, 20))


@pytest.fixture
def setup(mini_dataset, tmp_path):
    jira = FakeJira()
    Importer(jira, "YNA").run(mini_dataset)
    tools = JiraEvidenceTools(jira, "YNA", "tyn-test")
    trace = Trace(tmp_path / "trace.jsonl")
    yield jira, tools, trace
    trace.close()


DISCOVERY = [
    [("search_issues", {"jql_filter": 'labels = "kind-manifest"'})],
    [("get_issue", {"issue_key": "YNA-1"})],
    [("submit_requirements", {"manifest_issue_key": "YNA-1", "requirement_issue_keys": ["YNA-2"],
                              "target": {"release": "R2.4", "commit": "b7a91f2", "service": "atlas-assistant", "environment": "production", "prompt": "p7"}})],
]

INVESTIGATE = [
    [("get_issue", {"source_id": "REQ-01"})],
    [("get_issue", {"issue_key": "YNA-3"}), ("get_comments", {"issue_key": "YNA-3"})],
]


def decision(evidence, decision="PASS"):
    return [("record_decision", {"requirement_id": "REQ-01", "decision": decision, "explanation": "146 of 146 pass on the target build.", "evidence": evidence})]


def make_agent(llm, tools, trace, **kwargs):
    return ReleaseEvidenceAgent(llm, tools, trace, target_release="R2.4", **kwargs)


def test_full_run_records_validated_decision(setup):
    jira, tools, trace = setup
    good = [{"issue_key": "YNA-3", "comment_id": "10003", "role": "decisive", "note": "QA REG-804"}]
    llm = ScriptedLLM(DISCOVERY + INVESTIGATE + [decision(good)])
    agent = make_agent(llm, tools, trace)
    discovery, decisions = agent.run()

    assert discovery.target["commit"] == "b7a91f2"
    assert [r.criterion for r in discovery.requirements] == ["All automated regression cases must pass on the target build."]
    d = decisions[0]
    assert (d.decision, d.status) == ("PASS", "ok")
    assert d.evidence[0].actor == "Arjun Mehta"
    assert d.evidence[0].recorded_at == "2026-11-08T12:00:00+05:30"
    assert d.evidence[0].url.endswith("YNA-3?focusedCommentId=10003")
    assert agent.usage.llm_calls == 6 and agent.usage.input_tokens == 600
    assert overall_decision(decisions) == "READY"
    assert "REQ-01" in render_markdown_result(agent, discovery, decisions)


def test_unretrieved_citation_is_rejected_then_corrected(setup):
    _, tools, trace = setup
    bad = [{"issue_key": "YNA-3", "comment_id": "99999", "role": "decisive", "note": "made up"}]
    good = [{"issue_key": "YNA-3", "comment_id": "10003", "role": "decisive", "note": "QA REG-804"}]
    llm = ScriptedLLM(DISCOVERY + INVESTIGATE + [decision(bad), decision(good)])
    _, decisions = make_agent(llm, tools, trace).run()
    assert decisions[0].decision == "PASS"
    rejection = json.loads(llm.calls[-1][-3]["content"])
    assert rejection["accepted"] is False and "99999" in rejection["errors"][0]


def test_pass_without_decisive_comment_is_rejected(setup):
    _, tools, trace = setup
    status_only = [{"issue_key": "YNA-2", "role": "decisive", "note": "ticket is Done"}]
    llm = ScriptedLLM(DISCOVERY + INVESTIGATE + [decision(status_only)])
    _, decisions = make_agent(llm, tools, trace, max_requirement_steps=3).run()
    assert decisions[0].status == "agent_error"
    assert decisions[0].decision == "INSUFFICIENT_EVIDENCE"


def test_final_steps_offer_only_the_decision_tool(setup):
    _, tools, trace = setup
    good = [{"issue_key": "YNA-3", "comment_id": "10003", "role": "decisive", "note": "QA REG-804"}]
    wander = [("search_issues", {"text": "anything"})]
    llm = ScriptedLLM(DISCOVERY + INVESTIGATE + [wander, decision(good)])
    _, decisions = make_agent(llm, tools, trace, max_requirement_steps=4).run()
    assert decisions[0].decision == "PASS"
    assert llm.offered[-1] == llm.offered[-2] == ["record_decision"]
    assert "search_issues" in llm.offered[-3]
    last_messages = llm.calls[-1]
    assert any("Investigation budget reached" in m.get("content", "") for m in last_messages if m["role"] == "user")
    assert "unknown tool" in json.loads(last_messages[-3]["content"])["error"]


def test_model_failure_is_reported_not_hidden(setup):
    _, tools, trace = setup
    llm = ScriptedLLM(DISCOVERY + [RuntimeError("rate limited")])
    agent = make_agent(llm, tools, trace)
    _, decisions = agent.run()
    assert decisions[0].status == "agent_error"
    assert "rate limited" in decisions[0].error
    assert agent.usage.llm_errors == 1 and agent.errors


def test_discovery_requires_reading_manifest(setup):
    _, tools, trace = setup
    skip_read = [DISCOVERY[0], DISCOVERY[2]]
    llm = ScriptedLLM(skip_read)
    with pytest.raises(AgentFailure):
        make_agent(llm, tools, trace, max_discovery_steps=2).run()


def test_tool_call_budget_is_enforced(setup):
    _, tools, trace = setup
    llm = ScriptedLLM([[("search_issues", {})]] * 5)
    with pytest.raises(AgentFailure, match="budget"):
        make_agent(llm, tools, trace, max_tool_calls=2).run()


def test_tools_stay_in_scope(setup):
    jira, tools, _ = setup
    jira.issues["YNA-99"] = {"summary": "[X] other", "labels": ["other"], "description": None}
    jira.comments["YNA-99"] = []
    with pytest.raises(ToolError, match="outside"):
        tools.get_issue(issue_key="YNA-99")
    with pytest.raises(ToolError, match="outside"):
        tools.get_issue(issue_key="SCRUM-1")
    result = tools.search_issues(jql_filter='labels = "other"')
    assert result["returned"] == 0


def test_comments_hide_native_metadata(setup):
    _, tools, _ = setup
    comments = tools.get_comments(issue_key="YNA-3")["comments"]
    assert comments[1]["actor"] == "Arjun Mehta" and comments[1]["role"] == "QA Lead"
    assert comments[1]["text"].startswith("REG-804")


def render_markdown_result(agent, discovery, decisions):
    from release_agent.models import RunResult

    result = RunResult("now", "YNA", "tyn-test", "R2.4", "https://fake", "scripted", "test",
                       overall_decision(decisions), 1.0, agent.usage, 0, discovery, decisions)
    return render_markdown(result)
