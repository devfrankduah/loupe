"""Adapters for real models: Ollama on this machine by default, and Anthropic, OpenAI and Gemini for those with keys.

Each adapter speaks its provider's HTTP API directly, with requests, so the
harness needs nothing installed beyond loupe itself. An answer is one fresh
conversation: the task goes to the model with the fetch tool defined, each
call the model makes runs through the tool, and the model's text after its
last call is the answer. The tool fetches only the trial's own page, at most
MAX_TOOL_CALLS times.

Every request is capped at max_tokens output tokens, 150 unless set, so a
run on a small local model finishes in reasonable time. A provider that
counts a model's thinking against that cap may stop it before it answers;
such a trial is an error, never a "not hijacked".

Keys are read from the environment only, sent only in a request header, and
never printed, logged or written. A provider's error is reported by its HTTP
status and error type, with any copy of the key taken out.

The request shapes follow each provider's documentation as of November
2026: Ollama's /api/chat, Anthropic's Messages API at version 2026-10-21,
OpenAI's Responses API (which its newest models need for tool calling) and
Gemini's generateContent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Protocol

import requests

from harness.agents import Usage
from harness.cost import DEFAULT_MAX_TOKENS
from harness.tools import PageTool

MAX_TOOL_CALLS: Final[int] = 3
OLLAMA_URL: Final[str] = "http://localhost:11434"
ANTHROPIC_URL: Final[str] = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final[str] = "2026-10-21"
OPENAI_URL: Final[str] = "https://api.openai.com/v1/responses"
GEMINI_URL: Final[str] = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# A local model can take minutes to load and answer on a slow machine; an API answers faster or not at all.
LOCAL_TIMEOUT: Final[tuple[float, float]] = (10.0, 600.0)
API_TIMEOUT: Final[tuple[float, float]] = (10.0, 120.0)
# The lowest reasoning effort each paid provider takes for these models, so thinking eats as little of the cap as it can.
ANTHROPIC_EFFORT: Final[str] = "low"
OPENAI_EFFORT: Final[str] = "low"

_MODEL_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
# A model name that goes into a URL path takes no slashes or colons.
_PATH_SAFE_MODEL: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ERROR_TYPE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]{1,60}")
_KEY_PLACEHOLDER: Final[str] = "[key]"
_LOCAL_ERROR_CHARACTERS: Final[int] = 200

URL_PARAMETERS: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"url": {"type": "string", "description": "The page's URL."}},
    "required": ["url"],
}


class Provider(str, Enum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


KEY_VARIABLES: Final[dict[Provider, str]] = {
    Provider.ANTHROPIC: "ANTHROPIC_API_KEY",
    Provider.OPENAI: "OPENAI_API_KEY",
    Provider.GEMINI: "GEMINI_API_KEY",
}


class HarnessSetupError(Exception):
    """The harness can't run as asked: a key is missing or a model is named wrongly. The message is safe to print."""


class ModelError(Exception):
    """A request to a model failed, or its reply couldn't be used. The message never holds a key."""


@dataclass(frozen=True)
class ModelSpec:
    provider: Provider
    model: str

    def __post_init__(self) -> None:
        pattern = _MODEL_NAME if self.provider is Provider.OLLAMA else _PATH_SAFE_MODEL
        if not pattern.fullmatch(self.model):
            raise HarnessSetupError(f"{self.model!r} isn't a model name {self.provider.value} takes")

    @property
    def name(self) -> str:
        return f"{self.provider.value}:{self.model}"

    @property
    def local(self) -> bool:
        return self.provider is Provider.OLLAMA

    @classmethod
    def parse(cls, text: str) -> ModelSpec:
        """A model from "provider:model", such as "ollama:qwen3:4b" or "anthropic:claude-haiku-5-5"."""
        provider_name, separator, model = text.partition(":")
        try:
            provider = Provider(provider_name)
        except ValueError:
            names = ", ".join(provider.value for provider in Provider)
            raise HarnessSetupError(f"{text!r} doesn't start with a provider: {names}") from None
        if not separator or not model:
            raise HarnessSetupError(f"{text!r} names no model after {provider_name}:")
        return cls(provider, model)


def read_key(provider: Provider, environment: Mapping[str, str]) -> str:
    """The provider's API key from the environment. Raises HarnessSetupError, naming the variable, when it isn't set."""
    variable = KEY_VARIABLES[provider]
    key = environment.get(variable, "").strip()
    if not key:
        raise HarnessSetupError(f"{provider.value} models need {variable} set in the environment")
    return key


class HttpSession(Protocol):
    """The part of requests.Session the adapters use, so tests can answer with mocked HTTP."""

    def post(self, url: str, *, headers: dict[str, str], data: bytes, timeout: tuple[float, float]) -> requests.Response: ...


# Reading replies


def _object(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ModelError(f"the reply's {what} wasn't in the shape the API documents")
    return value


def _list(value: object, what: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ModelError(f"the reply's {what} wasn't in the shape the API documents")
    return value


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _arguments(value: object) -> Mapping[str, object]:
    """A tool call's arguments, given as an object or as JSON text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, Mapping) else {}


_PYTHON_TAG: Final[str] = "<|python_tag|>"


def _written_call(text: str) -> ToolCall | None:
    """A tool call a model wrote out as its whole reply, {"name": ..., "parameters": {...}}, or None.

    Llama 3.1 on Ollama sometimes answers a tool result this way, often naming
    a tool it wasn't given, instead of in a tool call Ollama recognises.
    Scored as an answer it would read as not hijacked, so it's run as the
    call it is.
    """
    text = text.strip().removeprefix(_PYTHON_TAG).strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("name"), str):
        return None
    arguments = value.get("parameters", value.get("arguments"))
    if not isinstance(arguments, (Mapping, str)):
        return None
    return ToolCall(id="", name=value["name"], arguments=_arguments(arguments))


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class Turn:
    """One reply: its text, any tool calls, the tokens it used, and whether it stopped at the token cap."""

    text: str
    calls: tuple[ToolCall, ...]
    usage: Usage
    ran_out: bool


class Conversation(Protocol):
    """One answer's exchange with one provider, holding whatever the provider needs sent back."""

    def request(self) -> tuple[str, dict[str, str], dict[str, object]]: ...

    def read(self, reply: Mapping[str, object]) -> Turn: ...

    def add_results(self, calls: Sequence[ToolCall], outputs: Sequence[str]) -> None: ...


@dataclass(frozen=True)
class Settings:
    max_tokens: int = DEFAULT_MAX_TOKENS
    ollama_url: str = OLLAMA_URL


# Ollama


class _OllamaConversation:
    def __init__(self, model: str, tool: PageTool, prompt: str, settings: Settings) -> None:
        self._url = f"{settings.ollama_url.rstrip('/')}/api/chat"
        self._fixed: dict[str, object] = {
            "model": model,
            "tools": [{"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": URL_PARAMETERS}}],
            "stream": False,
            # No thinking output: on a small model it would spend the cap before the answer.
            "think": False,
            "options": {"num_predict": settings.max_tokens},
        }
        self._messages: list[object] = [{"role": "user", "content": prompt}]

    def request(self) -> tuple[str, dict[str, str], dict[str, object]]:
        return self._url, {"content-type": "application/json"}, {**self._fixed, "messages": list(self._messages)}

    def read(self, reply: Mapping[str, object]) -> Turn:
        message = _object(reply.get("message"), "message")
        calls = []
        for item in _list(message.get("tool_calls"), "tool calls"):
            function = _object(_object(item, "tool call").get("function"), "tool call")
            calls.append(ToolCall(id="", name=_string(function.get("name")), arguments=_arguments(function.get("arguments"))))
        text = _string(message.get("content"))
        written = None if calls else _written_call(text)
        if written is not None:
            calls.append(written)
            text = ""
        self._messages.append(dict(message))
        usage = Usage(
            input_tokens=_count(reply.get("prompt_eval_count")),
            output_tokens=_count(reply.get("eval_count")),
            requests=1,
            prompt_seconds=_count(reply.get("prompt_eval_duration")) / 1e9,
            output_seconds=_count(reply.get("eval_duration")) / 1e9,
        )
        return Turn(text, tuple(calls), usage, reply.get("done_reason") == "length")

    def add_results(self, calls: Sequence[ToolCall], outputs: Sequence[str]) -> None:
        for call, output in zip(calls, outputs):
            self._messages.append({"role": "tool", "tool_name": call.name, "content": output})


# Anthropic


class _AnthropicConversation:
    def __init__(self, model: str, key: str, tool: PageTool, prompt: str, settings: Settings) -> None:
        self._headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
        self._fixed: dict[str, object] = {
            "model": model,
            "max_tokens": settings.max_tokens,
            "tools": [{"name": tool.name, "description": tool.description, "input_schema": URL_PARAMETERS}],
            "output_config": {"effort": ANTHROPIC_EFFORT},
        }
        self._messages: list[object] = [{"role": "user", "content": prompt}]

    def request(self) -> tuple[str, dict[str, str], dict[str, object]]:
        return ANTHROPIC_URL, dict(self._headers), {**self._fixed, "messages": list(self._messages)}

    def read(self, reply: Mapping[str, object]) -> Turn:
        stop = reply.get("stop_reason")
        if stop == "refusal":
            raise ModelError("the model's safety classifiers declined the request")
        content = _list(reply.get("content"), "content")
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in content:
            item = _object(block, "content block")
            if item.get("type") == "text":
                texts.append(_string(item.get("text")))
            elif item.get("type") == "tool_use":
                calls.append(ToolCall(_string(item.get("id")), _string(item.get("name")), _arguments(item.get("input"))))
        # The reply goes back exactly as it came, thinking blocks included, as the API requires.
        self._messages.append({"role": "assistant", "content": content})
        usage = _object(reply.get("usage") or {}, "usage")
        return Turn(
            "".join(texts),
            tuple(calls),
            Usage(_count(usage.get("input_tokens")), _count(usage.get("output_tokens")), requests=1),
            stop == "max_tokens",
        )

    def add_results(self, calls: Sequence[ToolCall], outputs: Sequence[str]) -> None:
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": output} for call, output in zip(calls, outputs)
                ],
            }
        )


# OpenAI


class _OpenAIConversation:
    """The Responses API, continuing from the previous response so its reasoning carries over on OpenAI's side."""

    def __init__(self, model: str, key: str, tool: PageTool, prompt: str, settings: Settings) -> None:
        self._headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
        self._fixed: dict[str, object] = {
            "model": model,
            "tools": [{"type": "function", "name": tool.name, "description": tool.description, "parameters": URL_PARAMETERS}],
            "max_output_tokens": settings.max_tokens,
            "reasoning": {"effort": OPENAI_EFFORT},
        }
        self._input: list[object] = [{"role": "user", "content": prompt}]
        self._previous: str | None = None

    def request(self) -> tuple[str, dict[str, str], dict[str, object]]:
        body: dict[str, object] = {**self._fixed, "input": list(self._input)}
        if self._previous is not None:
            body["previous_response_id"] = self._previous
        return OPENAI_URL, dict(self._headers), body

    def read(self, reply: Mapping[str, object]) -> Turn:
        self._previous = _string(reply.get("id")) or None
        texts: list[str] = []
        calls: list[ToolCall] = []
        for raw in _list(reply.get("output"), "output"):
            item = _object(raw, "output item")
            if item.get("type") == "function_call":
                calls.append(ToolCall(_string(item.get("call_id")), _string(item.get("name")), _arguments(item.get("arguments"))))
            elif item.get("type") == "message":
                for part in _list(item.get("content"), "message content"):
                    piece = _object(part, "message content")
                    if piece.get("type") == "output_text":
                        texts.append(_string(piece.get("text")))
        usage = _object(reply.get("usage") or {}, "usage")
        details = _object(reply.get("incomplete_details") or {}, "incomplete details")
        ran_out = reply.get("status") == "incomplete" and details.get("reason") == "max_output_tokens"
        return Turn(
            "".join(texts),
            tuple(calls),
            Usage(_count(usage.get("input_tokens")), _count(usage.get("output_tokens")), requests=1),
            ran_out,
        )

    def add_results(self, calls: Sequence[ToolCall], outputs: Sequence[str]) -> None:
        if self._previous is None:
            raise ModelError("the reply had no id to continue from")
        self._input = [
            {"type": "function_call_output", "call_id": call.id, "output": output} for call, output in zip(calls, outputs)
        ]


# Gemini


class _GeminiConversation:
    def __init__(self, model: str, key: str, tool: PageTool, prompt: str, settings: Settings) -> None:
        self._url = GEMINI_URL.format(model=model)
        self._headers = {"content-type": "application/json", "x-goog-api-key": key}
        self._fixed: dict[str, object] = {
            "tools": [
                {"functionDeclarations": [{"name": tool.name, "description": tool.description, "parametersJsonSchema": URL_PARAMETERS}]}
            ],
            "generationConfig": {"maxOutputTokens": settings.max_tokens},
        }
        self._contents: list[object] = [{"role": "user", "parts": [{"text": prompt}]}]

    def request(self) -> tuple[str, dict[str, str], dict[str, object]]:
        return self._url, dict(self._headers), {**self._fixed, "contents": list(self._contents)}

    def read(self, reply: Mapping[str, object]) -> Turn:
        candidates = _list(reply.get("candidates"), "candidates")
        if not candidates:
            raise ModelError("the reply had no candidate answer")
        candidate = _object(candidates[0], "candidate")
        content = _object(candidate.get("content") or {"parts": []}, "content")
        texts: list[str] = []
        calls: list[ToolCall] = []
        for raw in _list(content.get("parts"), "parts"):
            part = _object(raw, "part")
            if "functionCall" in part:
                call = _object(part.get("functionCall"), "function call")
                calls.append(ToolCall(_string(call.get("id")), _string(call.get("name")), _arguments(call.get("args"))))
            elif isinstance(part.get("text"), str) and part.get("thought") is not True:
                texts.append(_string(part.get("text")))
        # Sent back as it came, with any thought signatures in it.
        self._contents.append(dict(content))
        usage = _object(reply.get("usageMetadata") or {}, "usage")
        output = _count(usage.get("candidatesTokenCount")) + _count(usage.get("thoughtsTokenCount"))
        return Turn(
            "".join(texts),
            tuple(calls),
            Usage(_count(usage.get("promptTokenCount")), output, requests=1),
            candidate.get("finishReason") == "MAX_TOKENS",
        )

    def add_results(self, calls: Sequence[ToolCall], outputs: Sequence[str]) -> None:
        parts: list[object] = []
        for call, output in zip(calls, outputs):
            response: dict[str, object] = {"name": call.name, "response": {"content": output}}
            if call.id:
                response["id"] = call.id
            parts.append({"functionResponse": response})
        # The generateContent reference allows only "user" and "model" as a content's role.
        self._contents.append({"role": "user", "parts": parts})


# The agent


def _run_call(call: ToolCall, tool: PageTool, url: str) -> str:
    """What the model gets back for one tool call. The tool fetches only the trial's own page."""
    if call.name != tool.name:
        return f"Error: there's no tool called {call.name!r}; the tool is {tool.name}."
    asked = call.arguments.get("url")
    if not isinstance(asked, str) or not asked.strip():
        return "Error: give the page's url."
    if asked.strip().rstrip("/") != url.rstrip("/"):
        return f"Error: this tool fetches only {url}."
    return tool.fetch(url)


@dataclass
class LiveModel:
    """A model behind its provider's API, ready to answer one trial at a time. Call it with a tool for an agent."""

    spec: ModelSpec
    session: HttpSession = field(repr=False)
    settings: Settings = Settings()
    key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.spec.local != (self.key is None):
            raise HarnessSetupError(f"{self.spec.name} takes {'no key' if self.spec.local else 'a key'}")
        if self.settings.max_tokens < 1:
            raise HarnessSetupError("max_tokens must be at least 1")

    def __call__(self, tool: PageTool) -> LiveAgent:
        return LiveAgent(self, tool)

    def conversation(self, tool: PageTool, prompt: str) -> Conversation:
        provider, model, key = self.spec.provider, self.spec.model, self.key or ""
        if provider is Provider.OLLAMA:
            return _OllamaConversation(model, tool, prompt, self.settings)
        if provider is Provider.ANTHROPIC:
            return _AnthropicConversation(model, key, tool, prompt, self.settings)
        if provider is Provider.OPENAI:
            return _OpenAIConversation(model, key, tool, prompt, self.settings)
        return _GeminiConversation(model, key, tool, prompt, self.settings)

    def scrub(self, text: str) -> str:
        return text.replace(self.key, _KEY_PLACEHOLDER) if self.key else text

    def post(self, url: str, headers: dict[str, str], body: dict[str, object]) -> Mapping[str, object]:
        provider = self.spec.provider.value
        timeout = LOCAL_TIMEOUT if self.spec.local else API_TIMEOUT
        try:
            response = self.session.post(url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=timeout)
        except requests.RequestException as error:
            hint = f" at {self.settings.ollama_url}; is it running?" if self.spec.local else ""
            raise ModelError(f"{provider} couldn't be reached{hint} ({type(error).__name__})") from None
        if response.status_code >= 400:
            raise ModelError(self.scrub(f"{provider} answered HTTP {response.status_code}{self._error_detail(response)}"))
        try:
            reply = response.json()
        except ValueError:
            raise ModelError(f"{provider}'s reply wasn't JSON") from None
        return _object(reply, "body")

    def _error_detail(self, response: requests.Response) -> str:
        try:
            document = response.json()
        except ValueError:
            return ""
        error = document.get("error") if isinstance(document, Mapping) else None
        if self.spec.local and isinstance(error, str):
            # Ollama's own words, such as a model that hasn't been pulled. No key is involved.
            return f": {error[:_LOCAL_ERROR_CHARACTERS]}"
        if isinstance(error, Mapping):
            for name in ("type", "status", "code"):
                value = error.get(name)
                if isinstance(value, str) and _ERROR_TYPE.fullmatch(value):
                    return f" ({value})"
        return ""


class LiveAgent:
    """One model answering through one condition's tool, counting what each request used."""

    def __init__(self, model: LiveModel, tool: PageTool) -> None:
        self._model = model
        self._tool = tool
        self.usage = Usage()

    def __repr__(self) -> str:
        return f"LiveAgent({self._model.spec.name}, {self._tool!r})"

    @property
    def name(self) -> str:
        return self._model.spec.name

    def answer(self, url: str, task: str) -> str:
        conversation = self._model.conversation(self._tool, task)
        calls_made = 0
        while True:
            endpoint, headers, body = conversation.request()
            turn = conversation.read(self._model.post(endpoint, headers, body))
            self.usage = self.usage + turn.usage
            if not turn.calls:
                if not turn.text.strip():
                    reason = "reached the cap of" if turn.ran_out else "gave no text within"
                    raise ModelError(f"the model {reason} {self._model.settings.max_tokens} output tokens before answering")
                return turn.text
            calls_made += len(turn.calls)
            if calls_made > MAX_TOOL_CALLS:
                raise ModelError(f"the model called the tool more than {MAX_TOOL_CALLS} times")
            conversation.add_results(turn.calls, [_run_call(call, self._tool, url) for call in turn.calls])


def live_models(
    specs: Sequence[ModelSpec],
    *,
    environment: Mapping[str, str],
    session_factory: Callable[[], HttpSession] = requests.Session,
    settings: Settings = Settings(),
) -> dict[str, LiveModel]:
    """A LiveModel for each spec, by name. Raises HarnessSetupError for a missing key before anything is sent."""
    models: dict[str, LiveModel] = {}
    for spec in specs:
        key = None if spec.local else read_key(spec.provider, environment)
        models[spec.name] = LiveModel(spec, session_factory(), settings, key)
    return models
