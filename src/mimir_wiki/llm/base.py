from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from mimir_wiki.config import AppConfig, LLMConfig


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        error_type: str = "llm_error",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.error_type = error_type


class MissingCredentialsError(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False, error_type="missing_credentials")


class ContextLengthError(LLMError):
    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False, error_type="context_length_exceeded")


@dataclass(frozen=True)
class LLMRequest:
    task: str
    prompt: str
    document_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached: bool = False


class LLMProvider(Protocol):
    provider_name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class DisabledProvider:
    provider_name = "none"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMError("LLM provider is disabled", retryable=False, error_type="provider_disabled")


class ChatCompletionProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        url: str,
        headers: dict[str, str],
        default_model: str,
        timeout_seconds: float,
        model_field: str = "model",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.url = url
        self.headers = headers
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.model_field = model_field
        self.transport = transport

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model
        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only valid JSON. Ground every claim in the supplied document."
                    ),
                },
                {"role": "user", "content": request.prompt},
            ],
            "temperature": 0,
        }
        if self.model_field:
            payload[self.model_field] = model
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                response = await client.post(self.url, headers=self.headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError("LLM request timed out", retryable=True, error_type="timeout") from exc
        except httpx.TransportError as exc:
            raise LLMError(
                f"LLM connection error: {exc}", retryable=True, error_type="connection_error"
            ) from exc
        if response.status_code >= 400:
            raise _http_error(response)
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or choice.get("text") or ""
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=str(data.get("model") or model),
            input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
            output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
        )


def _http_error(response: httpx.Response) -> LLMError:
    retry_after: float | None = None
    retry_after_value = response.headers.get("Retry-After")
    if retry_after_value:
        try:
            retry_after = float(retry_after_value)
        except ValueError:
            retry_after = None
    body = response.text[:1000]
    lowered = body.lower()
    if "context" in lowered and "length" in lowered:
        return ContextLengthError("Provider reported context length exceeded")
    retryable = response.status_code in {408, 409, 429, 500, 502, 503, 504}
    error_type = "http_transient" if retryable else "http_non_retryable"
    if response.status_code in {401, 403}:
        error_type = "authorization_failed"
    elif response.status_code == 404:
        error_type = "model_or_deployment_not_found"
    elif response.status_code == 400:
        error_type = "invalid_request"
    return LLMError(
        f"Provider returned HTTP {response.status_code}: {body}",
        retryable=retryable,
        status_code=response.status_code,
        retry_after=retry_after,
        error_type=error_type,
    )


class RateLimitedLLMClient:
    def __init__(
        self,
        provider: LLMProvider,
        config: LLMConfig,
        retry_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrency))
        self._request_timestamps: list[float] = []
        self.retry_callback = retry_callback

    async def complete(self, request: LLMRequest) -> tuple[LLMResponse, int, int]:
        attempts = 0
        retries = 0
        async with self._semaphore:
            while True:
                attempts += 1
                await self._respect_request_limit()
                try:
                    response = await asyncio.wait_for(
                        self.provider.complete(request), timeout=self.config.timeout_seconds
                    )
                    return response, attempts, retries
                except TimeoutError as exc:
                    error = LLMError("LLM request timed out", retryable=True, error_type="timeout")
                    if not self._should_retry(error, attempts):
                        raise error from exc
                    retries += 1
                    await self._sleep_for_retry(error, attempts, request)
                except LLMError as exc:
                    if not self._should_retry(exc, attempts):
                        raise
                    retries += 1
                    await self._sleep_for_retry(exc, attempts, request)

    async def _respect_request_limit(self) -> None:
        rpm = self.config.requests_per_minute
        if not rpm:
            return
        now = time.monotonic()
        self._request_timestamps = [stamp for stamp in self._request_timestamps if now - stamp < 60]
        if len(self._request_timestamps) >= rpm:
            sleep_for = 60 - (now - self._request_timestamps[0])
            await asyncio.sleep(max(0, sleep_for))
        self._request_timestamps.append(time.monotonic())

    def _should_retry(self, error: LLMError, attempts: int) -> bool:
        if attempts > self.config.max_retries:
            return False
        if (
            error.status_code is not None
            and error.status_code in self.config.retryable_status_codes
        ):
            return True
        return error.retryable

    async def _sleep_for_retry(self, error: LLMError, attempts: int, request: LLMRequest) -> None:
        if self.config.respect_retry_after and error.retry_after is not None:
            delay = min(self.config.max_backoff_seconds, error.retry_after)
        else:
            delay = min(
                self.config.max_backoff_seconds,
                self.config.initial_backoff_seconds * (2 ** max(0, attempts - 1)),
            )
            if self.config.backoff_jitter:
                delay *= random.uniform(0.5, 1.5)
        if self.retry_callback:
            self.retry_callback(
                {
                    "event": "llm_retry",
                    "provider": self.provider.provider_name,
                    "task": request.task,
                    "document_id": request.document_id,
                    "model": request.model,
                    "attempt": attempts,
                    "status_code": error.status_code,
                    "error_type": error.error_type,
                    "retry_after": error.retry_after,
                    "sleep_seconds": round(delay, 3),
                }
            )
        await asyncio.sleep(delay)


def _required_env(env_name: str | None, description: str) -> str:
    if not env_name:
        raise MissingCredentialsError(
            f"Missing configured environment variable name for {description}"
        )
    value = os.environ.get(env_name)
    if not value:
        raise MissingCredentialsError(f"Required environment variable is not set: {env_name}")
    return value


def provider_for_config(config: AppConfig) -> LLMProvider:
    provider_name = config.llm.provider
    if provider_name == "none":
        return DisabledProvider()
    if provider_name == "openai":
        api_key = _required_env(config.llm.openai.get("api_key_env"), "OpenAI API key")
        return ChatCompletionProvider(
            provider_name="openai",
            url="https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            default_model=config.llm.model,
            timeout_seconds=config.llm.timeout_seconds,
        )
    if provider_name == "azure-openai":
        endpoint = _required_env(
            config.llm.azure_openai.get("endpoint_env"), "Azure OpenAI endpoint"
        )
        api_key = _required_env(config.llm.azure_openai.get("api_key_env"), "Azure OpenAI API key")
        deployment = (
            os.environ.get(config.llm.azure_openai.get("deployment_env", "")) or config.llm.model
        )
        api_version = _required_env(
            config.llm.azure_openai.get("api_version_env"), "Azure OpenAI API version"
        )
        url = (
            f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"
            f"/chat/completions?api-version={api_version}"
        )
        return ChatCompletionProvider(
            provider_name="azure-openai",
            url=url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            default_model=deployment,
            timeout_seconds=config.llm.timeout_seconds,
            model_field="",
        )
    if provider_name == "openai-compatible":
        base_url = _required_env(
            config.llm.openai_compatible.get("base_url_env"), "OpenAI-compatible base URL"
        )
        api_key = _required_env(
            config.llm.openai_compatible.get("api_key_env"), "OpenAI-compatible API key"
        )
        model_env = config.llm.openai_compatible.get("model_env")
        model = os.environ.get(model_env, config.llm.model) if model_env else config.llm.model
        return ChatCompletionProvider(
            provider_name="openai-compatible",
            url=f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            default_model=model,
            timeout_seconds=config.llm.timeout_seconds,
        )
    if provider_name == "azure-ai-foundry":
        endpoint = _required_env(
            config.llm.azure_ai_foundry.get("endpoint_env"), "Azure AI Foundry endpoint"
        )
        api_key = _required_env(
            config.llm.azure_ai_foundry.get("api_key_env"), "Azure AI Foundry API key"
        )
        deployment_env = config.llm.azure_ai_foundry.get("deployment_env")
        model = (
            os.environ.get(deployment_env, config.llm.model) if deployment_env else config.llm.model
        )
        url = endpoint.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        return ChatCompletionProvider(
            provider_name="azure-ai-foundry",
            url=url,
            headers={"api-key": api_key, "Authorization": f"Bearer {api_key}"},
            default_model=model,
            timeout_seconds=config.llm.timeout_seconds,
        )
    raise MissingCredentialsError(f"Unsupported provider configuration: {provider_name}")


def provider_for_name(provider_name: str) -> LLMProvider:
    if provider_name == "none":
        return DisabledProvider()
    raise MissingCredentialsError(f"Use provider_for_config for provider: {provider_name}")
