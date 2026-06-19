from __future__ import annotations

import base64
import json
import os
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from mimir_wiki.config import AppConfig
from mimir_wiki.llm.base import LLMError, MissingCredentialsError

EXPECTED_OCR_TEXT = "MIMIR 42"


@dataclass(frozen=True)
class ProbeEndpoint:
    provider: str
    api_kind: Literal["chat_completions", "responses"]
    url: str
    headers: dict[str, str]
    model: str
    include_model_field: bool = True


def build_probe_endpoint(config: AppConfig) -> ProbeEndpoint:
    provider = config.llm.provider
    if provider == "none":
        raise MissingCredentialsError(
            "Set --provider or llm.provider to a live provider for OCR probing"
        )
    if provider == "openai":
        api_key = _required_env(config.llm.openai.get("api_key_env"), "OpenAI API key")
        return ProbeEndpoint(
            provider=provider,
            api_kind="chat_completions",
            url="https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            model=config.llm.model,
        )
    if provider == "azure-openai":
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
        return ProbeEndpoint(
            provider=provider,
            api_kind="chat_completions",
            url=url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            model=deployment,
            include_model_field=False,
        )
    if provider == "openai-compatible":
        base_url = _required_env(
            config.llm.openai_compatible.get("base_url_env"), "OpenAI-compatible base URL"
        )
        api_key = _required_env(
            config.llm.openai_compatible.get("api_key_env"), "OpenAI-compatible API key"
        )
        model_env = config.llm.openai_compatible.get("model_env")
        model = os.environ.get(model_env, config.llm.model) if model_env else config.llm.model
        return ProbeEndpoint(
            provider=provider,
            api_kind="chat_completions",
            url=f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            model=model,
        )
    if provider == "azure-ai-foundry":
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
            return ProbeEndpoint(
                provider=provider,
                api_kind="responses",
                url=url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                model=model,
            )
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        return ProbeEndpoint(
            provider=provider,
            api_kind="chat_completions",
            url=url,
            headers={"api-key": api_key, "Authorization": f"Bearer {api_key}"},
            model=model,
        )
    raise MissingCredentialsError(f"Unsupported provider configuration: {provider}")


async def probe_multimodal_ocr(
    config: AppConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    endpoint = build_probe_endpoint(config)
    image_url = f"data:image/png;base64,{base64.b64encode(generate_probe_png()).decode('ascii')}"
    payload = build_probe_payload(endpoint, image_url)
    try:
        async with httpx.AsyncClient(
            timeout=config.llm.timeout_seconds, transport=transport
        ) as client:
            response = await client.post(endpoint.url, headers=endpoint.headers, json=payload)
    except httpx.TimeoutException as exc:
        raise LLMError("OCR probe request timed out", retryable=True, error_type="timeout") from exc
    except httpx.TransportError as exc:
        raise LLMError(
            f"OCR probe connection error: {exc}", retryable=True, error_type="connection_error"
        ) from exc

    result: dict[str, Any] = {
        "provider": endpoint.provider,
        "model": endpoint.model,
        "api_kind": endpoint.api_kind,
        "status_code": response.status_code,
        "expected_text": EXPECTED_OCR_TEXT,
        "image_input_accepted": response.status_code < 400,
        "ocr_text_matched": False,
        "response_text": "",
    }
    if response.status_code >= 400:
        result.update(
            {
                "status": "unsupported_or_failed",
                "error_type": _error_type(response),
                "error": response.text[:1000],
            }
        )
        return result

    data = response.json()
    response_text = extract_response_text(data)
    result["response_text"] = response_text[:1000]
    result["ocr_text_matched"] = normalized_contains(response_text, EXPECTED_OCR_TEXT)
    result["status"] = "ok" if result["ocr_text_matched"] else "image_accepted_ocr_mismatch"
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        result["usage"] = usage
    return result


def build_probe_payload(endpoint: ProbeEndpoint, image_url: str) -> dict[str, Any]:
    prompt = 'Read the text in the image. Return only JSON with this shape: {"image_text":"..."}'
    if endpoint.api_kind == "responses":
        return {
            "model": endpoint.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
        }
    payload: dict[str, Any] = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }
    if endpoint.include_model_field:
        payload["model"] = endpoint.model
    return payload


def extract_response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = data.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
                elif isinstance(content, str):
                    texts.append(content)
        if texts:
            return "\n".join(texts)
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
    return ""


def normalized_contains(response_text: str, expected: str) -> bool:
    found = response_text
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("image_text"), str):
        found = parsed["image_text"]

    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.lower())

    return normalize(expected) in normalize(found)


def generate_probe_png(text: str = EXPECTED_OCR_TEXT, *, scale: int = 6, margin: int = 8) -> bytes:
    glyphs = [_glyph(char) for char in text.upper()]
    glyph_width = 5
    glyph_height = 7
    spacing = 1
    width_units = sum(glyph_width if glyph else 3 for glyph in glyphs) + spacing * (len(glyphs) - 1)
    width = width_units * scale + margin * 2
    height = glyph_height * scale + margin * 2
    pixels = bytearray([255] * width * height * 3)
    x_units = 0
    for glyph in glyphs:
        char_width = glyph_width if glyph else 3
        if glyph:
            for y, row in enumerate(glyph):
                for x, bit in enumerate(row):
                    if bit != "1":
                        continue
                    _fill_rect(
                        pixels, width, margin + (x_units + x) * scale, margin + y * scale, scale
                    )
        x_units += char_width + spacing
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        start = y * width * 3
        raw.extend(pixels[start : start + width * 3])
    return (
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0), header=True)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )


def _fill_rect(pixels: bytearray, width: int, x0: int, y0: int, size: int) -> None:
    for y in range(y0, y0 + size):
        for x in range(x0, x0 + size):
            index = (y * width + x) * 3
            pixels[index : index + 3] = b"\x00\x00\x00"


def _png_chunk(chunk_type: bytes, data: bytes, *, header: bool = False) -> bytes:
    chunk = struct.pack(">I", len(data)) + chunk_type + data
    chunk += struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    if header:
        return b"\x89PNG\r\n\x1a\n" + chunk
    return chunk


def _glyph(char: str) -> list[str]:
    font = {
        "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
        "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
        "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
        "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
        "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    }
    return font.get(char, [])


def _required_env(env_name: str | None, description: str) -> str:
    if not env_name:
        raise MissingCredentialsError(
            f"Missing configured environment variable name for {description}"
        )
    value = os.environ.get(env_name)
    if not value:
        raise MissingCredentialsError(f"Required environment variable is not set: {env_name}")
    return value


def _error_type(response: httpx.Response) -> str:
    body = response.text.lower()
    if response.status_code in {401, 403}:
        return "authorization_failed"
    if response.status_code == 404:
        return "model_or_deployment_not_found"
    if response.status_code == 400 and any(
        marker in body for marker in ("image", "vision", "modal", "content type", "input_image")
    ):
        return "image_input_unsupported"
    if response.status_code == 400:
        return "invalid_request"
    if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
        return "http_transient"
    return "http_non_retryable"
