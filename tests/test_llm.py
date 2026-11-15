import json

import httpx
import pytest

from release_agent.agent import ReleaseEvidenceAgent
from release_agent.llm import (
    LLMError,
    LLMNotConfigured,
    OpenAICompatibleLLM,
    ToolCall,
    ToolSpec,
    create_llm,
    parse_openai_response,
    to_openai_messages,
)

TOOL = ToolSpec("get_issue", "Read an issue.", {"type": "object", "properties": {"issue_key": {"type": "string"}}})


def _completion(message, usage=(50, 10), finish="tool_calls"):
    return {"id": "x", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}}


def _llm(handler, monkeypatch, **kwargs):
    monkeypatch.setattr("release_agent.llm.time.sleep", lambda _: None)
    return OpenAICompatibleLLM("nvidia", "m", "key", "https://api.test/v1", transport=httpx.MockTransport(handler), **kwargs)


def test_message_conversion_round_trip():
    call = ToolCall("call_1", "get_issue", {"issue_key": "YNA-1"})
    converted = to_openai_messages("sys", [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "name": "get_issue", "content": "{}"},
    ])
    assert converted[0] == {"role": "system", "content": "sys"}
    assert converted[2]["tool_calls"][0]["function"] == {"name": "get_issue", "arguments": '{"issue_key": "YNA-1"}'}
    assert converted[3] == {"role": "tool", "tool_call_id": "call_1", "content": "{}"}


def test_parse_tool_calls_and_usage():
    response = parse_openai_response(_completion({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_issue", "arguments": '{"issue_key": "YNA-2"}'}},
        {"id": "c2", "type": "function", "function": {"name": "get_issue", "arguments": "{broken"}},
    ]}))
    assert response.tool_calls[0].arguments == {"issue_key": "YNA-2"}
    assert response.tool_calls[1].error and response.tool_calls[1].arguments == {}
    assert (response.usage.input_tokens, response.usage.output_tokens) == (50, 10)
    assert response.stop_reason == "tool_calls"


def test_request_body_and_auth(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion({"role": "assistant", "content": "done"}, finish="stop"))

    response = _llm(handler, monkeypatch).complete("sys", [{"role": "user", "content": "hi"}], [TOOL])
    assert response.text == "done"
    assert seen["auth"] == "Bearer key"
    assert seen["body"]["tools"][0]["function"]["name"] == "get_issue"
    assert seen["body"]["stream"] is False and seen["body"]["temperature"] == 0.0


def test_retries_rate_limit(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="slow down")
        return httpx.Response(200, json=_completion({"role": "assistant", "content": "ok"}, finish="stop"))

    assert _llm(handler, monkeypatch).complete("s", [{"role": "user", "content": "x"}], []).text == "ok"
    assert len(attempts) == 3


def test_gives_up_after_retries(monkeypatch):
    llm = _llm(lambda r: httpx.Response(503, text="down"), monkeypatch, max_retries=2)
    with pytest.raises(LLMError, match="after 2 retries"):
        llm.complete("s", [{"role": "user", "content": "x"}], [])


def test_client_error_is_not_retried(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(422, text="bad field")

    with pytest.raises(LLMError, match="422"):
        _llm(handler, monkeypatch).complete("s", [{"role": "user", "content": "x"}], [])
    assert len(attempts) == 1


def test_polls_pending_response(monkeypatch):
    polls = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, headers={"NVCF-REQID": "req-1"})
        polls.append(str(request.url))
        if len(polls) < 2:
            return httpx.Response(202, headers={"NVCF-REQID": "req-1"})
        return httpx.Response(200, json=_completion({"role": "assistant", "content": "late"}, finish="stop"))

    assert _llm(handler, monkeypatch).complete("s", [{"role": "user", "content": "x"}], []).text == "late"
    assert polls[0].endswith("/status/req-1")


def test_create_llm_validation():
    with pytest.raises(LLMNotConfigured, match="No LLM provider"):
        create_llm("placeholder", "", "")
    with pytest.raises(LLMNotConfigured, match="LLM_API_KEY"):
        create_llm("nvidia", "meta/llama-3.3-70b-instruct", "")
    with pytest.raises(LLMNotConfigured, match="LLM_BASE_URL"):
        create_llm("openai-compatible", "m", "k")
    assert create_llm("nvidia", "m", "k").provider == "nvidia"


def test_invalid_arguments_reach_model_as_tool_error(tmp_path):
    from release_agent.trace import Trace

    agent = ReleaseEvidenceAgent(None, None, Trace(tmp_path / "t.jsonl"), target_release="R2.4")
    content, payload = agent._dispatch(ToolCall("c", "get_issue", {}, error="bad json"), {"get_issue": lambda **_: {}},
                                       "record_decision", "requirement", "REQ-01", 1, agent.usage)
    assert payload is None and "not a valid JSON object" in json.loads(content)["error"]
    agent.trace.close()
