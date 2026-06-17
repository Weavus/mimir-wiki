from __future__ import annotations

import asyncio
import json

import httpx

from mimir_wiki.config import LLMConfig
from mimir_wiki.llm.base import (
    ChatCompletionProvider,
    ContextLengthError,
    LLMError,
    LLMRequest,
    LLMResponse,
    RateLimitedLLMClient,
    ResponsesProvider,
)


class FlakyProvider:
    provider_name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.calls < 3:
            raise LLMError(
                "rate limited",
                retryable=True,
                status_code=429,
                retry_after=0,
                error_type="rate_limit",
            )
        return LLMResponse(text="ok", model=request.model or "mock")


class BadRequestProvider:
    provider_name = "bad"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMError(
            "bad request", retryable=False, status_code=400, error_type="invalid_request"
        )


async def _complete_with_flaky() -> tuple[str, int, int, int]:
    provider = FlakyProvider()
    client = RateLimitedLLMClient(
        provider,
        LLMConfig(
            max_retries=3,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
            provider="openai",
            model="mock",
        ),
    )
    response, attempts, retries = await client.complete(
        LLMRequest(task="summary", prompt="x", model="mock")
    )
    return response.text, attempts, retries, provider.calls


async def _complete_with_bad_request() -> None:
    client = RateLimitedLLMClient(
        BadRequestProvider(),
        LLMConfig(
            max_retries=3,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
            provider="openai",
            model="mock",
        ),
    )
    await client.complete(LLMRequest(task="summary", prompt="x", model="mock"))


def test_rate_limited_client_retries_transient_errors() -> None:
    text, attempts, retries, calls = asyncio.run(_complete_with_flaky())
    assert text == "ok"
    assert attempts == 3
    assert retries == 2
    assert calls == 3


def test_rate_limited_client_emits_retry_events() -> None:
    events = []
    provider = FlakyProvider()

    async def run() -> None:
        client = RateLimitedLLMClient(
            provider,
            LLMConfig(
                max_retries=3,
                initial_backoff_seconds=0,
                max_backoff_seconds=0,
                provider="openai",
                model="mock",
            ),
            retry_callback=events.append,
        )
        await client.complete(
            LLMRequest(task="summary", prompt="x", document_id="doc-1", model="mock")
        )

    asyncio.run(run())
    assert len(events) == 2
    assert events[0]["event"] == "llm_retry"
    assert events[0]["document_id"] == "doc-1"


def test_rate_limited_client_does_not_retry_non_retryable_errors() -> None:
    try:
        asyncio.run(_complete_with_bad_request())
    except LLMError as exc:
        assert exc.status_code == 400
    else:  # pragma: no cover
        raise AssertionError("expected LLMError")


async def _complete_with_mock_transport(
    statuses: list[int],
) -> tuple[LLMResponse, int, int, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        status = statuses.pop(0)
        if status == 200:
            return httpx.Response(
                200,
                json={
                    "model": "mock-model",
                    "choices": [{"message": {"content": '{"summary": "ok"}'}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            )
        return httpx.Response(status, headers={"Retry-After": "0"}, text="rate limited")

    provider = ChatCompletionProvider(
        provider_name="openai",
        url="https://example.test/chat/completions",
        headers={"Authorization": "Bearer test"},
        default_model="mock-model",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    client = RateLimitedLLMClient(
        provider,
        LLMConfig(
            provider="openai",
            model="mock-model",
            max_retries=3,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
    )
    response, attempts, retries = await client.complete(
        LLMRequest(task="summary", prompt="hello", model="mock-model")
    )
    return response, attempts, retries, requests


def test_chat_completion_provider_success_and_retry_after() -> None:
    response, attempts, retries, requests = asyncio.run(_complete_with_mock_transport([429, 200]))
    assert response.text == '{"summary": "ok"}'
    assert response.input_tokens == 11
    assert response.output_tokens == 7
    assert attempts == 2
    assert retries == 1
    assert requests[0]["model"] == "mock-model"


async def _complete_with_error(status: int, body: str) -> None:
    provider = ChatCompletionProvider(
        provider_name="openai",
        url="https://example.test/chat/completions",
        headers={"Authorization": "Bearer test"},
        default_model="mock-model",
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body)),
    )
    await provider.complete(LLMRequest(task="summary", prompt="hello", model="mock-model"))


def test_chat_completion_provider_detects_context_length_error() -> None:
    try:
        asyncio.run(_complete_with_error(400, "context length exceeded"))
    except ContextLengthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ContextLengthError")


def test_chat_completion_provider_detects_auth_failure() -> None:
    try:
        asyncio.run(_complete_with_error(401, "bad key"))
    except LLMError as exc:
        assert exc.error_type == "authorization_failed"
        assert exc.retryable is False
    else:  # pragma: no cover
        raise AssertionError("expected LLMError")


async def _complete_with_responses_provider() -> tuple[LLMResponse, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "model": "gpt-5.5",
                "output": [
                    {"content": [{"type": "output_text", "text": '{"short_summary":"ok"}'}]}
                ],
                "usage": {"input_tokens": 13, "output_tokens": 5},
            },
        )

    provider = ResponsesProvider(
        provider_name="azure-ai-foundry",
        url="https://example.services.ai.azure.com/openai/v1/responses",
        headers={"Authorization": "Bearer test"},
        default_model="gpt-5.5",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    response = await provider.complete(LLMRequest(task="summary", prompt="hello"))
    return response, requests


def test_responses_provider_posts_model_and_extracts_output_text() -> None:
    response, requests = asyncio.run(_complete_with_responses_provider())
    assert response.text == '{"short_summary":"ok"}'
    assert response.model == "gpt-5.5"
    assert response.input_tokens == 13
    assert response.output_tokens == 5
    assert requests[0]["model"] == "gpt-5.5"
    assert "hello" in requests[0]["input"]
