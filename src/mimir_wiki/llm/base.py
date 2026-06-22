from __future__ import annotations

import asyncio
import math
import os
import random
import time
from collections.abc import Awaitable, Callable
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
    payload: dict[str, Any] | None = None
    estimated_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached: bool = False


@dataclass
class _TokenReservation:
    timestamp: float
    tokens: int


@dataclass
class _AdaptiveConcurrencyState:
    current_limit: int
    in_flight: int = 0
    successes_since_increase: int = 0
    cooldown_until: float = 0
    cooldown_seconds: float = 0


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


class ResponsesProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        url: str,
        headers: dict[str, str],
        default_model: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.url = url
        self.headers = headers
        self.default_model = default_model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.default_model
        payload = {
            "model": model,
            "input": (
                "Return only valid JSON. Ground every claim in the supplied document.\n\n"
                f"{request.prompt}"
            ),
        }
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
        usage = data.get("usage") or {}
        return LLMResponse(
            text=_extract_responses_text(data),
            model=str(data.get("model") or model),
            input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        )


def _extract_responses_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = data.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        texts.append(part)
                    elif isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
            elif isinstance(content, str):
                texts.append(content)
            elif isinstance(item.get("text"), str):
                texts.append(item["text"])
        if texts:
            return "\n".join(texts)
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                return content
    return ""


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
        monotonic: Callable[[], float] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.provider = provider
        self.config = config
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrency))
        self._request_timestamps: list[float] = []
        self._token_reservations: list[_TokenReservation] = []
        self._adaptive_states: dict[str, _AdaptiveConcurrencyState] = {}
        self._adaptive_condition = asyncio.Condition()
        self._rate_lock = asyncio.Lock()
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep_func or asyncio.sleep
        self.retry_callback = retry_callback

    async def complete(self, request: LLMRequest) -> tuple[LLMResponse, int, int]:
        attempts = 0
        retries = 0
        async with self._semaphore:
            while True:
                attempts += 1
                model_key = self._model_key(request)
                await self._acquire_adaptive_slot(model_key)
                async with self._rate_lock:
                    await self._respect_request_limit()
                    token_reservation = await self._respect_token_limit(request)
                try:
                    self._emit_provider_event("llm_provider_call_started", request, model_key)
                    response = await asyncio.wait_for(
                        self.provider.complete(request), timeout=self.config.timeout_seconds
                    )
                    await self._release_adaptive_slot(model_key, success=True)
                    self._record_actual_token_usage(token_reservation, response)
                    self._emit_provider_event(
                        "llm_provider_call_finished",
                        request,
                        model_key,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                    )
                    return response, attempts, retries
                except TimeoutError as exc:
                    error = LLMError("LLM request timed out", retryable=True, error_type="timeout")
                    await self._release_adaptive_slot(model_key, error=error)
                    self._emit_provider_event(
                        "llm_provider_call_failed", request, model_key, error=error
                    )
                    if not self._should_retry(error, attempts):
                        raise error from exc
                    retries += 1
                    await self._sleep_for_retry(error, attempts, request)
                except LLMError as exc:
                    await self._release_adaptive_slot(model_key, error=exc)
                    self._emit_provider_event(
                        "llm_provider_call_failed", request, model_key, error=exc
                    )
                    if not self._should_retry(exc, attempts):
                        raise
                    retries += 1
                    await self._sleep_for_retry(exc, attempts, request)
                except Exception as exc:
                    error = LLMError(str(exc), retryable=False, error_type=type(exc).__name__)
                    await self._release_adaptive_slot(model_key, error=error)
                    self._emit_provider_event(
                        "llm_provider_call_failed", request, model_key, error=error
                    )
                    raise

    def _model_key(self, request: LLMRequest) -> str:
        return f"{self.provider.provider_name}:{request.model or self.config.model}"

    def _emit_provider_event(
        self,
        event: str,
        request: LLMRequest,
        model_key: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error: LLMError | None = None,
    ) -> None:
        if self.retry_callback is None:
            return
        self.retry_callback(
            {
                "event": event,
                "provider": self.provider.provider_name,
                "task": request.task,
                "document_id": request.document_id,
                "model": request.model,
                "model_key": model_key,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "status_code": error.status_code if error is not None else None,
                "error_type": error.error_type if error is not None else None,
            }
        )

    def _adaptive_state(self, model_key: str) -> _AdaptiveConcurrencyState:
        state = self._adaptive_states.get(model_key)
        if state is not None:
            return state
        max_limit = max(1, self.config.max_concurrency)
        initial_limit = min(max_limit, max(1, self.config.adaptive_initial_concurrency))
        state = _AdaptiveConcurrencyState(
            current_limit=initial_limit,
            cooldown_seconds=max(0, self.config.adaptive_cooldown_seconds_on_429),
        )
        self._adaptive_states[model_key] = state
        return state

    async def _acquire_adaptive_slot(self, model_key: str) -> None:
        if not self.config.adaptive_concurrency:
            return
        while True:
            sleep_for: float | None = None
            async with self._adaptive_condition:
                state = self._adaptive_state(model_key)
                now = self._monotonic()
                if state.cooldown_until > now:
                    sleep_for = state.cooldown_until - now
                elif state.in_flight < state.current_limit:
                    state.in_flight += 1
                    return
                else:
                    await self._adaptive_condition.wait()
                    continue
            if sleep_for is not None:
                await self._sleep(max(0, sleep_for))

    async def _release_adaptive_slot(
        self,
        model_key: str,
        *,
        success: bool = False,
        error: LLMError | None = None,
    ) -> None:
        if not self.config.adaptive_concurrency:
            return
        async with self._adaptive_condition:
            state = self._adaptive_state(model_key)
            state.in_flight = max(0, state.in_flight - 1)
            if success:
                self._record_adaptive_success(model_key, state)
            elif error is not None:
                self._record_adaptive_error(model_key, state, error)
            self._adaptive_condition.notify_all()

    def _record_adaptive_success(self, model_key: str, state: _AdaptiveConcurrencyState) -> None:
        max_limit = max(1, self.config.max_concurrency)
        if state.current_limit >= max_limit:
            return
        state.successes_since_increase += 1
        threshold = max(1, self.config.adaptive_increase_after_successes)
        if state.successes_since_increase < threshold:
            return
        old_limit = state.current_limit
        state.current_limit = min(max_limit, state.current_limit + 1)
        state.successes_since_increase = 0
        state.cooldown_seconds = max(0, self.config.adaptive_cooldown_seconds_on_429)
        self._emit_adaptive_event(model_key, "concurrency_increased", old_limit, state)

    def _record_adaptive_error(
        self, model_key: str, state: _AdaptiveConcurrencyState, error: LLMError
    ) -> None:
        if error.status_code != 429 and error.error_type not in {"rate_limit", "http_429"}:
            return
        old_limit = state.current_limit
        min_limit = max(1, self.config.adaptive_min_concurrency)
        decrease_factor = min(1, max(0.1, self.config.adaptive_decrease_factor_on_429))
        state.current_limit = max(min_limit, int(state.current_limit * decrease_factor))
        state.successes_since_increase = 0
        now = self._monotonic()
        base_cooldown = max(0, self.config.adaptive_cooldown_seconds_on_429)
        if error.retry_after is not None:
            cooldown = max(0, error.retry_after)
        else:
            cooldown = state.cooldown_seconds or base_cooldown
        cooldown = min(max(0, self.config.adaptive_max_cooldown_seconds), cooldown)
        state.cooldown_until = max(state.cooldown_until, now + cooldown)
        state.cooldown_seconds = min(
            max(0, self.config.adaptive_max_cooldown_seconds),
            max(base_cooldown, cooldown * 2 if cooldown else base_cooldown),
        )
        self._emit_adaptive_event(model_key, "concurrency_decreased", old_limit, state)

    def _emit_adaptive_event(
        self,
        model_key: str,
        event: str,
        old_limit: int,
        state: _AdaptiveConcurrencyState,
    ) -> None:
        if self.retry_callback is None or old_limit == state.current_limit:
            return
        self.retry_callback(
            {
                "event": "llm_adaptive_concurrency",
                "adaptive_event": event,
                "provider": self.provider.provider_name,
                "model_key": model_key,
                "old_concurrency": old_limit,
                "new_concurrency": state.current_limit,
                "cooldown_until": round(state.cooldown_until, 3),
            }
        )

    async def _respect_request_limit(self) -> None:
        rpm = self.config.requests_per_minute
        if not rpm:
            return
        now = self._monotonic()
        self._request_timestamps = [stamp for stamp in self._request_timestamps if now - stamp < 60]
        if len(self._request_timestamps) >= rpm:
            sleep_for = 60 - (now - self._request_timestamps[0])
            await self._sleep(max(0, sleep_for))
            now = self._monotonic()
            self._request_timestamps = [
                stamp for stamp in self._request_timestamps if now - stamp < 60
            ]
        self._request_timestamps.append(self._monotonic())

    async def _respect_token_limit(self, request: LLMRequest) -> _TokenReservation | None:
        tpm = self.config.tokens_per_minute
        if not tpm:
            return None
        estimated_tokens = estimate_request_tokens(request)
        if estimated_tokens <= 0:
            return None
        now = self._monotonic()
        self._token_reservations = [
            reservation
            for reservation in self._token_reservations
            if now - reservation.timestamp < 60
        ]
        reserved_tokens = sum(reservation.tokens for reservation in self._token_reservations)
        if self._token_reservations and reserved_tokens + estimated_tokens > tpm:
            sleep_for = 60 - (now - self._token_reservations[0].timestamp)
            await self._sleep(max(0, sleep_for))
            now = self._monotonic()
            self._token_reservations = [
                reservation
                for reservation in self._token_reservations
                if now - reservation.timestamp < 60
            ]
        reservation = _TokenReservation(timestamp=self._monotonic(), tokens=estimated_tokens)
        self._token_reservations.append(reservation)
        return reservation

    def _record_actual_token_usage(
        self, reservation: _TokenReservation | None, response: LLMResponse
    ) -> None:
        if reservation is None:
            return
        if response.input_tokens is None and response.output_tokens is None:
            return
        reservation.tokens = max(0, (response.input_tokens or 0) + (response.output_tokens or 0))

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
        await self._sleep(delay)


def estimate_request_tokens(request: LLMRequest) -> int:
    if request.estimated_tokens is not None:
        return max(0, request.estimated_tokens)
    text_parts = [request.prompt]
    if request.payload:
        text_parts.extend(_payload_text_values(request.payload))
    text = "\n".join(part for part in text_parts if part)
    if not text:
        return 1
    return max(1, math.ceil(len(text) / 4))


def _payload_text_values(value: Any, *, parent_key: str = "") -> list[str]:
    if isinstance(value, dict):
        values: list[str] = []
        for key, item in value.items():
            values.extend(_payload_text_values(item, parent_key=str(key)))
        return values
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_payload_text_values(item, parent_key=parent_key))
        return values
    if not isinstance(value, str):
        return []
    if parent_key in {"image_url", "url"} and value.startswith("data:image/"):
        return []
    return [value]


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
        api_mode = str(config.llm.azure_ai_foundry.get("api_mode") or "auto")
        if api_mode == "responses" or url.endswith("/openai/v1") or "/openai/v1/" in url:
            if not url.endswith("/responses"):
                url = f"{url}/responses"
            return ResponsesProvider(
                provider_name="azure-ai-foundry",
                url=url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                default_model=model,
                timeout_seconds=config.llm.timeout_seconds,
            )
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
