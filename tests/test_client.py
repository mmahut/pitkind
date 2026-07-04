from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from pitkind.client import call_model
from pitkind.mock import get_stub_response


class _FakeStream:
    def __init__(self, chunks, content=None):
        self._chunks = list(chunks)
        self.content = content

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _usage_chunk():
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01}),
        id="gen-test",
    )


def _response(content, finish_reason=None):
    content_chunks = [] if content is None else [SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=finish_reason)],
        usage=None,
        id="gen-test",
    )]
    return _FakeStream(content_chunks + [_usage_chunk()], content=content)


@pytest.mark.parametrize("responses,status,calls", [
    ([_response("invalid"), "error", _response(get_stub_response(0, 1))], None, 3),
    ([_response("invalid"), _response(None), _response(get_stub_response(0, 1))], None, 3),
    ([_response(""), _response("")], "api_failure", 2),
    ([_response("invalid"), "error", "error"], "api_failure", 3),
    (["error", _response(get_stub_response(0, 1))], None, 2),
])
async def test_real_retry_path(monkeypatch, responses, status, calls):
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://example.test"))
    create = AsyncMock(side_effect=[error if r == "error" else r for r in responses])
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create = create

    def make_client(**kwargs):
        assert kwargs["max_retries"] == 0
        return client

    sleep = AsyncMock()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(openai, "AsyncOpenAI", make_client)
    monkeypatch.setattr("pitkind.client.asyncio.sleep", sleep)
    entry = await call_model("test", 0, "system", "user", {}, 1, 1)
    assert getattr(entry.output, "status", None) == status
    assert create.await_count == entry.usage.api_calls == calls
    successful_requests = sum(r != "error" for r in responses)
    assert entry.usage.prompt_tokens == successful_requests * 10
    assert entry.usage.cost_usd == pytest.approx(successful_requests * 0.01)
    assert sleep.await_count == (0 if calls == 2 and status == "schema_failure" else 1)
    assert entry.user_message == "user"
    assert entry.usage.generation_ids == ["gen-test"] * successful_requests
    assert len(entry.usage.attempts) == calls
    if responses[0] != 'error' and responses[0].content == 'invalid':
        assert entry.usage.attempts[0]['raw'] == 'invalid'
        assert 'validation_error' in entry.usage.attempts[0]
    client.__aexit__.assert_awaited_once()


async def test_completed_entry_is_persisted_by_callback():
    seen = []

    async def save(entry):
        seen.append(entry)

    entry = await call_model("test", 0, "system", "user", {}, 0, 0, True,
                             {}, {"round_name": "R0", "argument_ids": []}, save)
    assert seen == [entry]


async def test_schema_retry_carries_validation_feedback(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    create = AsyncMock(side_effect=[_response("invalid"), _response(get_stub_response(0, 1))])
    client.chat.completions.create = create
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test')
    monkeypatch.setattr(openai, 'AsyncOpenAI', lambda **kwargs: client)
    entry = await call_model('test', 0, 'system', 'user', {}, 1, 0)
    assert entry.output.verdict in ("YES", "NO", "ABSTAIN")
    retry_messages = create.await_args_list[1].kwargs["messages"]
    assert [m["role"] for m in retry_messages] == ["system", "user", "assistant", "user"]
    assert retry_messages[2]["content"] == "invalid"
    assert "failed output validation" in retry_messages[3]["content"]


async def test_midstream_transport_error_from_forked_httpx_is_retried(monkeypatch):
    httpx2 = pytest.importorskip("httpx2")

    class _BrokenStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body")

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create = AsyncMock(
        side_effect=[_BrokenStream(), _response(get_stub_response(0, 1))])
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test')
    monkeypatch.setattr(openai, 'AsyncOpenAI', lambda **kwargs: client)
    monkeypatch.setattr("pitkind.client.asyncio.sleep", AsyncMock())
    entry = await call_model('test', 0, 'system', 'user', {}, 0, 1)
    assert entry.output.verdict in ("YES", "NO", "ABSTAIN")
    assert "peer closed connection" in entry.usage.attempts[0]["error"]


async def test_length_response_is_rejected_even_with_valid_json(monkeypatch):
    raw = get_stub_response(0, 1)
    response = _response(raw, finish_reason='length')
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create.return_value = response
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test')
    monkeypatch.setattr(openai, 'AsyncOpenAI', lambda **kwargs: client)
    entry = await call_model('test', 0, 'system', 'user', {}, 2, 0)
    assert entry.output.status == 'incomplete_response'
    assert entry.output.raw == raw
    assert entry.usage.attempts[0]['raw'] == raw
    assert client.chat.completions.create.await_count == 1
