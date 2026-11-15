import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVCF_STATUS_URL = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/status/{request_id}"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    error: str | None = None


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: str | None = None


class LLMNotConfigured(RuntimeError):
    pass


class LLMError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class LLMClient(ABC):
    """Provider-neutral chat client.

    Messages are dicts: {"role": "user", "content": str},
    {"role": "assistant", "text": str, "tool_calls": [ToolCall]} or
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}.
    """

    provider: str = ""
    model: str = ""

    @abstractmethod
    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse: ...


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    converted = [{"role": "system", "content": system}]
    for message in messages:
        role = message["role"]
        if role == "user":
            converted.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            item = {"role": "assistant", "content": message.get("text") or ""}
            if message.get("tool_calls"):
                item["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in message["tool_calls"]
                ]
            converted.append(item)
        elif role == "tool":
            converted.append({"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]})
        else:
            raise ValueError(f"unknown message role '{role}'")
    return converted


def to_openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]


def _parse_arguments(raw: Any) -> tuple[dict, str | None]:
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, f"{exc}: {str(raw)[:200]}"
    if not isinstance(value, dict):
        return {}, f"expected a JSON object, got {type(value).__name__}"
    return value, None


def parse_openai_response(data: dict) -> LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(0, f"response has no choices: {str(data)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments, error = _parse_arguments(function.get("arguments"))
        calls.append(ToolCall(raw.get("id") or f"call_{index}", function.get("name") or "", arguments, error))
    usage = data.get("usage") or {}
    return LLMResponse(
        text=message.get("content") or "",
        tool_calls=calls,
        usage=TokenUsage(usage.get("prompt_tokens"), usage.get("completion_tokens")),
        stop_reason=choice.get("finish_reason"),
    )


class OpenAICompatibleLLM(LLMClient):
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        timeout: float = 120.0,
        max_retries: int = 5,
        max_poll_seconds: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.max_poll_seconds = max_poll_seconds
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._url = f"{base_url.rstrip('/')}/chat/completions"

    def close(self) -> None:
        self._http.close()

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMResponse:
        body = {
            "model": self.model,
            "messages": to_openai_messages(system, messages),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = to_openai_tools(tools)
        return parse_openai_response(self._post(body))

    def _post(self, body: dict) -> dict:
        status, detail = 0, ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.post(self._url, json=body)
                if resp.status_code == 202:
                    resp = self._poll(resp)
            except httpx.TransportError as exc:
                status, detail = 0, str(exc)
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 30))
                    continue
                break
            if resp.status_code == 429 or resp.status_code >= 500:
                status, detail = resp.status_code, resp.text[:300]
                if attempt < self.max_retries:
                    time.sleep(self._retry_delay(resp, attempt))
                    continue
                break
            if resp.status_code >= 400:
                raise LLMError(resp.status_code, f"{self.model} -> {resp.status_code}: {resp.text[:500]}")
            return resp.json()
        raise LLMError(status, f"{self.model} failed after {self.max_retries} retries: {status} {detail}")

    # NVIDIA cloud functions answer 202 with a request id when the result is not ready yet.
    def _poll(self, resp: httpx.Response) -> httpx.Response:
        request_id = resp.headers.get("NVCF-REQID")
        if not request_id:
            raise LLMError(202, "pending response without NVCF-REQID header")
        deadline = time.monotonic() + self.max_poll_seconds
        while resp.status_code == 202:
            if time.monotonic() > deadline:
                raise LLMError(202, f"request {request_id} still pending after {self.max_poll_seconds}s")
            time.sleep(1.0)
            resp = self._http.get(NVCF_STATUS_URL.format(request_id=request_id))
        return resp

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After", "")
        return float(header) if header.isdigit() else float(min(2 ** (attempt + 1), 60))


def create_llm(provider: str, model: str, api_key: str, base_url: str = "", temperature: float = 0.0, max_tokens: int = 2048, **options) -> LLMClient:
    provider = (provider or "placeholder").lower()
    if provider == "placeholder":
        raise LLMNotConfigured(
            "No LLM provider configured. Set LLM_PROVIDER, LLM_MODEL and LLM_API_KEY, "
            "and register an LLMClient adapter in release_agent/llm.py."
        )
    if provider not in ("nvidia", "openai-compatible"):
        raise LLMNotConfigured(f"Unsupported LLM_PROVIDER '{provider}'. Use 'nvidia' or 'openai-compatible'.")
    base_url = base_url or (NVIDIA_BASE_URL if provider == "nvidia" else "")
    missing = [name for name, value in (("LLM_MODEL", model), ("LLM_API_KEY", api_key), ("LLM_BASE_URL", base_url)) if not value]
    if missing:
        raise LLMNotConfigured(f"Missing {', '.join(missing)} for provider '{provider}'.")
    return OpenAICompatibleLLM(provider, model, api_key, base_url, temperature, max_tokens, **options)
