"""Thin wrapper around the Groq chat completions API (OpenAI-style messages and tools).

Tool definitions elsewhere in the app use the neutral shape
{"name", "description", "input_schema"}; they are converted here.
"""

import json
import logging
from functools import lru_cache

import groq

from app.config import settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_client() -> groq.Groq:
    return groq.Groq(max_retries=3, timeout=120.0)


def to_groq_tools(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tools
    ]


def create(*, system: str, messages: list, tools: list | None = None, effort: str | None = None,
           max_tokens: int = 8000, json_mode: bool = False, client=None):
    """One chat completion call. `messages` excludes the system prompt. Returns the ChatCompletion."""
    if client is None:
        if not settings.llm_enabled:
            raise LLMUnavailable("GROQ_API_KEY is not set")
        client = get_client()
    kwargs = dict(
        model=settings.llm_model,
        messages=[{"role": "system", "content": system}, *messages],
        max_completion_tokens=max_tokens,
    )
    # Reasoning effort is only accepted by reasoning models (gpt-oss, qwen3).
    if settings.llm_model.startswith(("openai/gpt-oss", "qwen/")):
        kwargs["reasoning_effort"] = effort or settings.llm_effort
    if tools:
        kwargs["tools"] = to_groq_tools(tools)
        kwargs["tool_choice"] = "auto"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(2):
        try:
            return client.chat.completions.create(**kwargs)
        except groq.BadRequestError as e:
            # Groq rejects a turn when the model emits a malformed tool call; one retry usually fixes it.
            if "tool_use_failed" in str(e) and attempt == 0:
                log.warning("Groq tool_use_failed, retrying once")
                continue
            log.error("Groq bad request: %s", e)
            raise LLMUnavailable("bad request") from e
        except groq.RateLimitError as e:
            log.warning("Groq rate limited: %s", e)
            raise LLMUnavailable("rate limited") from e
        except groq.APIConnectionError as e:
            log.warning("Groq connection error: %s", e)
            raise LLMUnavailable("connection error") from e
        except groq.APIStatusError as e:
            log.error("Groq API error %s: %s", e.status_code, e)
            raise LLMUnavailable(f"API error {e.status_code}") from e
    raise LLMUnavailable("tool call failed twice")


def text_of(response) -> str:
    return (response.choices[0].message.content or "").strip()


def complete_text(system: str, prompt: str, effort: str = "low", client=None) -> str:
    """Single-shot text generation (drafts, summaries)."""
    return text_of(create(system=system, messages=[{"role": "user", "content": prompt}], effort=effort, client=client))


def complete_json(system: str, prompt: str, schema: dict, effort: str = "low", client=None) -> dict:
    """Single-shot extraction as a JSON object with the keys of `schema`."""
    system = f"{system}\nRespond with only a JSON object matching this JSON schema:\n{json.dumps(schema)}"
    resp = create(system=system, messages=[{"role": "user", "content": prompt}], effort=effort,
                  json_mode=True, client=client)
    try:
        data = json.loads(text_of(resp))
    except json.JSONDecodeError as e:
        raise LLMUnavailable("model returned invalid JSON") from e
    return {k: data.get(k) for k in schema.get("properties", {})}



def assistant_entry(message) -> dict:
    """The assistant turn as we store and resend it (reasoning text is not sent back)."""
    entry: dict = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        entry["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in message.tool_calls
        ]
    return entry
