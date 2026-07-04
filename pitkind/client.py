from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

from .io_paths import utc_now
from .mock import get_stub_response
from .schemas import FailureRecord, ModelOutput, StageEntry, UsageRecord

_debug_log_path: Path | None = None

_PROVIDER_CONFIG = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY"),
}


def set_debug_log(path: Path | None) -> None:
    """Append one JSONL record per API call (full request and response) to path."""
    global _debug_log_path
    _debug_log_path = path


def _debug_write(record: dict[str, Any]) -> None:
    if _debug_log_path is None:
        return
    try:
        with _debug_log_path.open("a") as stream:
            stream.write(json.dumps(record, default=repr) + "\n")
    except OSError as exc:
        print(f"Debug log write failed: {exc}", file=sys.stderr)


async def _request(client, model_id: str, messages: list[dict[str, str]],
                   decoding_params: dict[str, Any], max_api_retries: int,
                   usage: UsageRecord, debug_context: dict[str, Any] | None = None) -> str | FailureRecord:
    """Apply the same API retry budget to every schema attempt.

    Responses are streamed: intermediaries on the provider path drop
    connections that stay silent for ~100 s, which long non-streaming
    deliberation calls always exceed.
    """
    import httpx
    from openai import APIError

    # The SDK may run on a forked httpx (httpx2); its transport errors are not
    # httpx.HTTPError subclasses and escape stream iteration unless caught too.
    recoverable: tuple[type[Exception], ...] = (APIError, httpx.HTTPError)
    try:
        import httpx2
    except ImportError:
        pass
    else:
        recoverable += (httpx2.HTTPError,)

    for attempt in range(max_api_retries + 1):
        usage.api_calls += 1
        record = {
            "at": utc_now().isoformat(),
            **(debug_context or {}),
            "api_attempt": attempt,
            "request": {"model": model_id, "messages": messages, "decoding_params": decoding_params},
        }
        retry_delay: float = 2 ** attempt
        try:
            # extra_body carries OpenRouter-specific params (e.g. reasoning
            # budgets) that the SDK method signature does not accept.
            params = dict(decoding_params)
            extra_body = params.pop("extra_body", None)
            stream = await client.chat.completions.create(
                model=model_id, messages=messages, stream=True,
                stream_options={"include_usage": True}, extra_body=extra_body, **params,
            )
            content_parts: list[str] = []
            finish_reason = native_finish = provider = response_id = None
            usage_data: dict[str, Any] | None = None
            chunk_count = 0
            has_refusal = has_tool_calls = False
            async for chunk in stream:
                chunk_count += 1
                response_id = getattr(chunk, "id", None) or response_id
                provider = getattr(chunk, "provider", None) or provider
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage_data = chunk_usage.model_dump()
                if getattr(chunk, "choices", None):
                    choice = chunk.choices[0]
                    finish_reason = getattr(choice, "finish_reason", None) or finish_reason
                    native_finish = getattr(choice, "native_finish_reason", None) or native_finish
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        if getattr(delta, "content", None):
                            content_parts.append(delta.content)
                        has_refusal = has_refusal or bool(getattr(delta, "refusal", None))
                        has_tool_calls = has_tool_calls or bool(getattr(delta, "tool_calls", None))
        except recoverable as exc:
            error = str(exc)
            usage.attempts.append({"error": error})
            _debug_write({**record, "api_error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "status_code": getattr(exc, "status_code", None),
                "body": getattr(exc, "body", None),
            }})
            if getattr(exc, "status_code", None) == 402:
                # Credit reservation conflict with concurrent in-flight
                # requests; give the other seats time to settle.
                retry_delay = 15
        else:
            if response_id:
                usage.generation_ids.append(response_id)
            if usage_data:
                usage.prompt_tokens += usage_data.get("prompt_tokens") or 0
                usage.completion_tokens += usage_data.get("completion_tokens") or 0
                if usage_data.get("cost") is not None:
                    usage.cost_usd = (usage.cost_usd or 0) + usage_data["cost"]
            content = "".join(content_parts)
            if finish_reason:
                usage.finish_reasons.append(finish_reason)
            usage.attempts.append({"generation_id": response_id, "finish_reason": finish_reason,
                                   "native_finish_reason": native_finish, "provider": provider,
                                   "raw": content})
            usage.response_diagnostics.append({
                "content_length": len(content),
                "has_refusal": has_refusal,
                "has_tool_calls": has_tool_calls,
            })
            _debug_write({**record, "response": {
                "id": response_id, "provider": provider, "finish_reason": finish_reason,
                "native_finish_reason": native_finish, "chunks": chunk_count,
                "usage": usage_data, "content": content,
            }})
            if finish_reason == "length":
                return FailureRecord(status="incomplete_response", raw=content,
                                     error="Provider reached the output token limit (finish_reason=length)")
            if content:
                return content
            error = f"provider returned empty content (finish_reason={finish_reason}, chunks={chunk_count})"
            usage.attempts[-1]["error"] = error
        if attempt < max_api_retries:
            await asyncio.sleep(retry_delay)
    return FailureRecord(status="api_failure", error=error)


async def call_model(
    model_id: str,
    model_index: int,
    system_message: str,
    user_message: str,
    decoding_params: dict[str, Any],
    max_retries: int,
    max_api_retries: int,
    mock: bool = False,
    _mock_call_count: dict | None = None,
    validation_context: dict[str, Any] | None = None,
    on_complete: Callable[[StageEntry], Awaitable[None]] | None = None,
    provider: str = "openrouter",
) -> StageEntry:
    started_at = utc_now()
    usage = UsageRecord(api_calls=0, prompt_tokens=0, completion_tokens=0)
    call_counts = _mock_call_count if _mock_call_count is not None else {}
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]

    async with AsyncExitStack() as stack:
        if not mock:
            from openai import AsyncOpenAI

            base_url, key_name = _PROVIDER_CONFIG[provider]
            client = await stack.enter_async_context(AsyncOpenAI(
                base_url=base_url,
                api_key=os.environ[key_name],
                max_retries=0,  # _request owns the retry budget.
            ))

        for attempt in range(max_retries + 1):
            if mock:
                call_counts[model_index] = call_counts.get(model_index, 0) + 1
                raw = get_stub_response(model_index, call_counts[model_index], validation_context)
            else:
                raw = await _request(client, model_id, messages, decoding_params, max_api_retries, usage,
                                     debug_context={
                                         "round": (validation_context or {}).get("round_name"),
                                         "seat_index": model_index,
                                         "schema_attempt": attempt,
                                     })
            if isinstance(raw, FailureRecord):
                output = raw
                break
            try:
                output = ModelOutput.model_validate_json(raw, context=validation_context)
                break
            except ValueError as exc:
                usage.validation_errors.append(str(exc))
                if usage.attempts:
                    usage.attempts[-1]["validation_error"] = str(exc)
                output = FailureRecord(
                    status="schema_failure", raw=raw, error=str(exc),
                )
                # At temperature 0 an unchanged retry repeats the same invalid
                # output; feed the validation error back instead.
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                        f"Your previous response failed output validation:\n{exc}\n"
                        "Return a corrected, complete JSON object that satisfies the output "
                        "schema and the stated deliberation rules. Do not repeat the invalid "
                        "parts unchanged and do not add commentary outside the JSON object."},
                ]

    entry = StageEntry(
        model=model_id, seat_index=model_index, started_at=started_at, completed_at=utc_now(),
        user_message=user_message, usage=None if mock else usage, output=output,
    )
    if on_complete is not None:
        await on_complete(entry)
    return entry
