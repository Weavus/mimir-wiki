from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import struct
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.llm.base import LLMError, LLMRequest, LLMResponse, RateLimitedLLMClient
from mimir_wiki.llm.probe import ProbeEndpoint, build_probe_endpoint, extract_response_text
from mimir_wiki.schemas import LLMUsage, VisualExtractionArtifact, VisualExtractionImage
from mimir_wiki.utils import atomic_write_json, load_json, utc_now

IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass(frozen=True)
class VisualSource:
    source: str
    source_kind: Literal["data_url", "file", "url"]
    mime_type: str | None = None
    source_order: int = 0
    nearby_heading: str | None = None
    context: str = ""
    selection_score: int = 0
    selection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class VisualSourceResult:
    image: VisualExtractionImage
    usage: LLMUsage | None = None
    retries: int = 0


class VisualPayloadProvider:
    def __init__(self, endpoint: ProbeEndpoint, llm_client: httpx.AsyncClient) -> None:
        self.provider_name = endpoint.provider
        self.endpoint = endpoint
        self.llm_client = llm_client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.payload is None:
            raise LLMError("Visual extraction request is missing payload", retryable=False)
        try:
            response = await self.llm_client.post(
                self.endpoint.url,
                headers=self.endpoint.headers,
                json=request.payload,
            )
        except httpx.TimeoutException as exc:
            raise LLMError(
                "Visual extraction timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise LLMError(
                f"Visual extraction connection error: {exc}",
                retryable=True,
                error_type="connection_error",
            ) from exc
        if response.status_code >= 400:
            raise visual_http_error(response)
        data = response.json()
        usage = data.get("usage") or {}
        return LLMResponse(
            text=extract_response_text(data),
            model=str(data.get("model") or request.model or self.endpoint.model),
            input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        )


def visual_extraction_path(bundle: PageBundle) -> Path:
    return bundle.paths.root / "visual_extraction.json"


def load_visual_extraction(bundle: PageBundle) -> VisualExtractionArtifact | None:
    path = visual_extraction_path(bundle)
    if not path.exists():
        return None
    try:
        return VisualExtractionArtifact.model_validate(load_json(path))
    except (OSError, ValueError):
        return None


def discover_visual_sources(
    bundle: PageBundle, *, max_images: int | None = None
) -> list[VisualSource]:
    sources: list[VisualSource] = []
    seen: set[str] = set()
    source_order = 0
    for match in MARKDOWN_IMAGE_RE.finditer(bundle.clean_markdown):
        source = match.group(1).strip("<>")
        if should_skip_image_source(source) or source in seen:
            continue
        source_order += 1
        visual_source = source_from_reference(
            bundle,
            source,
            source_order=source_order,
            nearby_heading=nearest_heading(bundle.clean_markdown, match.start()),
            context=nearby_context(bundle.clean_markdown, match.start()),
        )
        if visual_source.source in seen:
            continue
        seen.update({source, visual_source.source})
        sources.append(visual_source)
        if max_images is not None and len(sources) >= max_images:
            return sources
    if bundle.paths.attachments.exists():
        for path in sorted(bundle.paths.attachments.iterdir()):
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            source = str(path)
            if source in seen:
                continue
            source_order += 1
            seen.add(source)
            sources.append(
                VisualSource(
                    source=source,
                    source_kind="file",
                    mime_type=mime_type_for(path.name),
                    source_order=source_order,
                    context=path.name,
                )
            )
            if max_images is not None and len(sources) >= max_images:
                return sources
    return sources


def rank_visual_sources(bundle: PageBundle, sources: list[VisualSource]) -> list[VisualSource]:
    scored = [score_visual_source(bundle, source) for source in sources]
    return sorted(scored, key=lambda source: (-source.selection_score, source.source_order))


def effective_visual_page_cap(bundle: PageBundle, config: AppConfig) -> int:
    configured_cap = config.visual_extraction.max_images_per_page
    if configured_cap < 0 or not config.visual_extraction.adaptive_page_caps:
        return configured_cap
    if is_report_like_page(bundle):
        return min(configured_cap, config.visual_extraction.report_page_max_images)
    return configured_cap


def select_visual_sources(
    bundle: PageBundle, ranked_sources: list[VisualSource], *, cap: int, config: AppConfig
) -> tuple[list[VisualSource], list[VisualSource], list[VisualSource]]:
    selected: list[VisualSource] = []
    grouped_out: list[VisualSource] = []
    group_counts: Counter[str] = Counter()
    group_limit = config.visual_extraction.max_images_per_representative_group
    for source in ranked_sources:
        if cap >= 0 and len(selected) >= cap:
            continue
        group_key = representative_group_key(bundle, source, config=config)
        if group_key and group_counts[group_key] >= group_limit:
            grouped_out.append(source)
            continue
        selected.append(source)
        if group_key:
            group_counts[group_key] += 1
    selected_ids = {source.source for source in selected}
    grouped_ids = {source.source for source in grouped_out}
    capped_out = [
        source
        for source in ranked_sources
        if source.source not in selected_ids and source.source not in grouped_ids
    ]
    return selected, grouped_out, capped_out


def representative_group_key(
    bundle: PageBundle, source: VisualSource, *, config: AppConfig
) -> str | None:
    if not config.visual_extraction.representative_group_sampling:
        return None
    text = " ".join(
        part for part in [source.source, source.nearby_heading or "", source.context] if part
    ).lower()
    if not is_report_like_page(bundle) and not re.search(
        r"dashboard|chart|graph|metric|cloudwatch|splunk|grafana|kibana", text
    ):
        return None
    normalized = re.sub(r"\b\d{1,4}[-_:./]?\d{0,4}[-_:./]?\d{0,4}\b", "#", text)
    normalized = re.sub(r"image[-_. ]*#", "image", normalized)
    normalized = re.sub(r"[^a-z#]+", " ", normalized)
    tokens = [token for token in normalized.split() if token not in {"attachments", "pages"}]
    return " ".join(tokens[:12]) or None


def is_report_like_page(bundle: PageBundle) -> bool:
    text = " ".join(
        [bundle.metadata.title, *bundle.metadata.labels, *bundle.ancestor_titles]
    ).lower()
    return bool(re.search(r"\breport\b|weekly status|daily status|monthly status", text))


def score_visual_source(bundle: PageBundle, source: VisualSource) -> VisualSource:
    text = " ".join(
        part for part in [source.source, source.nearby_heading or "", source.context] if part
    ).lower()
    score = 10
    reasons: list[str] = []
    if source.context:
        score += 20
        reasons.append("markdown_reference")
    high_value_patterns = {
        "architecture_context": r"architecture|diagram|topology|network|schema|flow|sequence",
        "operational_context": r"runbook|procedure|recovery|diagnostic|validation|backout",
        "incident_context": (
            r"incident|investigation|root cause|rca|error|exception|failure|timeout"
        ),
        "monitoring_context": (
            r"dashboard|chart|metric|alarm|alert|monitor|cloudwatch|splunk|grafana|kibana"
        ),
        "code_or_log_context": r"terminal|command|powershell|shell|log|stack trace|json|xml|sql",
    }
    for reason, pattern in high_value_patterns.items():
        if re.search(pattern, text):
            score += 25
            reasons.append(reason)
    if bundle.text.strip() and len(bundle.text.split()) < 150:
        score += 15
        reasons.append("low_text_page")
    if re.search(r"logo|avatar|icon|badge|thumbnail|spacer|blank|placeholder", text):
        score -= 35
        reasons.append("likely_low_value_visual")
    if source.source_kind == "url":
        score -= 20
        reasons.append("remote_not_local")
    return VisualSource(
        source=source.source,
        source_kind=source.source_kind,
        mime_type=source.mime_type,
        source_order=source.source_order,
        nearby_heading=source.nearby_heading,
        context=source.context,
        selection_score=score,
        selection_reasons=tuple(reasons or ["default_order"]),
    )


def nearest_heading(markdown: str, position: int) -> str | None:
    prefix = markdown[:position]
    for line in reversed(prefix.splitlines()):
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
    return None


def nearby_context(markdown: str, position: int) -> str:
    line_start = markdown.rfind("\n", 0, position) + 1
    line_end = markdown.find("\n", position)
    if line_end == -1:
        line_end = len(markdown)
    previous_start = markdown.rfind("\n", 0, max(0, line_start - 1)) + 1
    next_end = markdown.find("\n", line_end + 1)
    if next_end == -1:
        next_end = len(markdown)
    start = previous_start if previous_start < line_start else line_start
    end = next_end if next_end > line_end else line_end
    return markdown[start:end]


def should_skip_image_source(source: str) -> bool:
    if not source or source.startswith("$"):
        return True
    parsed = urlparse(source)
    source_path = unquote(parsed.path or source).lower()
    if source_path.startswith("/images/icons/") or "/images/icons/" in source_path:
        return True
    if parsed.scheme in {"http", "https", "data", "file"}:
        return False
    return Path(unquote(source)).suffix.lower() not in IMAGE_EXTENSIONS


def source_from_reference(
    bundle: PageBundle,
    source: str,
    *,
    source_order: int = 0,
    nearby_heading: str | None = None,
    context: str = "",
) -> VisualSource:
    parsed = urlparse(source)
    if parsed.scheme == "data":
        mime_type = source.split(";", 1)[0].removeprefix("data:") or None
        return VisualSource(
            source=source,
            source_kind="data_url",
            mime_type=mime_type,
            source_order=source_order,
            nearby_heading=nearby_heading,
            context=context,
        )
    if parsed.scheme in {"http", "https"}:
        local_path = local_attachment_for_url(bundle, source)
        if local_path is not None:
            return VisualSource(
                source=str(local_path),
                source_kind="file",
                mime_type=mime_type_for(local_path.name),
                source_order=source_order,
                nearby_heading=nearby_heading,
                context=context,
            )
        return VisualSource(
            source=source,
            source_kind="url",
            mime_type=mime_type_for(parsed.path),
            source_order=source_order,
            nearby_heading=nearby_heading,
            context=context,
        )
    path = Path(unquote(source))
    if not path.is_absolute():
        path = bundle.paths.clean_md.parent / path
    return VisualSource(
        source=str(path),
        source_kind="file",
        mime_type=mime_type_for(path.name),
        source_order=source_order,
        nearby_heading=nearby_heading,
        context=context,
    )


async def extract_visuals_for_page(
    *,
    bundle: PageBundle,
    config: AppConfig,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    dry_run: bool = False,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    image_cache: dict[str, VisualExtractionImage] | None = None,
    sources: list[VisualSource] | None = None,
) -> tuple[VisualExtractionArtifact, int, list[LLMUsage], int]:
    endpoint = build_visual_probe_endpoint(config)
    if sources is None:
        discovered_sources = discover_visual_sources(bundle)
        ranked_sources = rank_visual_sources(bundle, discovered_sources)
        max_images = config.visual_extraction.max_images_per_page
        sources = ranked_sources[:max_images] if max_images >= 0 else ranked_sources
    if not sources:
        artifact = build_visual_artifact(
            bundle=bundle,
            run_id=run_id,
            dataset_name=dataset_name,
            generated_at=generated_at,
            endpoint=endpoint,
            prompt_version=config.visual_extraction.prompt_version,
            status="skipped",
            images=[],
        )
        if not dry_run:
            atomic_write_json(visual_extraction_path(bundle), artifact.model_dump(mode="json"))
            return artifact, 1, [], 0
        return artifact, 0, [], 0

    images: list[VisualExtractionImage] = []
    usage_records: list[LLMUsage] = []
    total_retries = 0
    async with httpx.AsyncClient(
        timeout=config.llm.timeout_seconds, transport=llm_transport
    ) as llm_client:
        provider = VisualPayloadProvider(endpoint, llm_client)
        rate_limited_client = RateLimitedLLMClient(
            provider,
            config.llm,
            retry_callback=progress_callback,
        )
        for index, source in enumerate(sources, start=1):
            image_id = visual_image_id(index, source)
            if progress_callback is not None:
                progress_callback(
                    {
                        "event": "visual_image_started",
                        "image_index": index,
                        "image_total": len(sources),
                        "image_id": image_id,
                        "image_source_kind": source.source_kind,
                    }
                )
            result = await extract_visual_source(
                index=index,
                source=source,
                config=config,
                endpoint=endpoint,
                llm_client=rate_limited_client,
                bundle=bundle,
                run_id=run_id,
                dataset_name=dataset_name,
                generated_at=generated_at,
                image_cache=image_cache,
            )
            image = result.image
            images.append(image)
            if result.usage is not None:
                usage_records.append(result.usage)
            total_retries += result.retries
            if progress_callback is not None:
                progress_callback(
                    {
                        "event": "visual_image_finished",
                        "image_index": index,
                        "image_total": len(sources),
                        "image_id": image.image_id,
                        "image_status": image.status,
                    }
                )
    succeeded = sum(1 for image in images if image.status == "success")
    failed = sum(1 for image in images if image.status == "failed")
    skipped = sum(1 for image in images if image.status == "skipped")
    status: Literal["complete", "partial", "failed", "skipped"]
    if succeeded == len(images):
        status = "complete"
    elif succeeded:
        status = "partial"
    elif skipped == len(images):
        status = "skipped"
    else:
        status = "failed"
    artifact = build_visual_artifact(
        bundle=bundle,
        run_id=run_id,
        dataset_name=dataset_name,
        generated_at=generated_at,
        endpoint=endpoint,
        prompt_version=config.visual_extraction.prompt_version,
        status=status,
        images=images,
        images_succeeded=succeeded,
        images_failed=failed,
        images_skipped=skipped,
    )
    if not dry_run:
        atomic_write_json(visual_extraction_path(bundle), artifact.model_dump(mode="json"))
        return artifact, 1, usage_records, total_retries
    return artifact, 0, usage_records, total_retries


async def extract_visual_source(
    *,
    index: int,
    source: VisualSource,
    config: AppConfig,
    endpoint: ProbeEndpoint,
    llm_client: RateLimitedLLMClient,
    bundle: PageBundle,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    image_cache: dict[str, VisualExtractionImage] | None = None,
) -> VisualSourceResult:
    image_id = visual_image_id(index, source)
    if source.source_kind == "url":
        return VisualSourceResult(
            VisualExtractionImage(
                image_id=image_id,
                source=source.source,
                source_kind=source.source_kind,
                mime_type=source.mime_type,
                status="skipped",
                error_type="remote_source_not_in_cache",
                error=(
                    "Remote image references are not fetched by mimir-wiki. "
                    "Run mimir-confluence with attachment export enabled, then rerun extraction."
                ),
                provider=endpoint.provider,
                model=endpoint.model,
                prompt_version=config.visual_extraction.prompt_version,
            ),
        )
    try:
        image_bytes, mime_type = load_image_bytes(source)
    except (OSError, ValueError) as exc:
        return VisualSourceResult(
            VisualExtractionImage(
                image_id=image_id,
                source=source.source,
                source_kind=source.source_kind,
                mime_type=source.mime_type,
                status="failed",
                error_type=type(exc).__name__,
                error=str(exc)[:1000],
                provider=endpoint.provider,
                model=endpoint.model,
                prompt_version=config.visual_extraction.prompt_version,
            )
        )
    source_hash = hashlib.sha256(image_bytes).hexdigest()
    low_value_reason = low_value_image_reason(source, image_bytes, config=config)
    if low_value_reason is not None:
        return VisualSourceResult(
            VisualExtractionImage(
                image_id=image_id,
                source=source.source,
                source_kind=source.source_kind,
                mime_type=mime_type,
                content_sha256=source_hash,
                status="skipped",
                caption=f"Skipped before OCR: {low_value_reason}.",
                error_type="low_value_visual",
                error=low_value_reason,
                provider=endpoint.provider,
                model=endpoint.model,
                prompt_version=config.visual_extraction.prompt_version,
            )
        )
    cached_image = image_cache.get(source_hash) if image_cache is not None else None
    if cached_image is not None:
        return VisualSourceResult(
            image=cached_image.model_copy(
                update={
                    "image_id": image_id,
                    "source": source.source,
                    "source_kind": source.source_kind,
                    "mime_type": mime_type,
                    "content_sha256": source_hash,
                    "cache_hit": True,
                }
            )
        )
    payload = build_visual_payload(
        endpoint,
        image_bytes=image_bytes,
        mime_type=mime_type,
        title_hint=Path(unquote(urlparse(source.source).path)).name,
    )
    started = time.monotonic()
    try:
        response, attempts, retries = await llm_client.complete(
            LLMRequest(
                task="visual_ocr",
                prompt="Extract source evidence from this Confluence image.",
                document_id=bundle.document_id,
                model=endpoint.model,
                prompt_version=config.visual_extraction.prompt_version,
                payload=payload,
            )
        )
    except LLMError as exc:
        error_type = f"http_{exc.status_code}" if exc.status_code is not None else exc.error_type
        return VisualSourceResult(
            VisualExtractionImage(
                image_id=image_id,
                source=source.source,
                source_kind=source.source_kind,
                mime_type=mime_type,
                content_sha256=source_hash,
                status="failed",
                error_type=error_type,
                error=str(exc)[:1000],
                provider=endpoint.provider,
                model=endpoint.model,
                prompt_version=config.visual_extraction.prompt_version,
            )
        )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    text = response.text
    parsed = parse_visual_response(text)
    ocr_text = parsed.get("ocr_text", "")
    caption = parsed.get("caption", "")
    confidence = parsed.get("confidence")
    if not ocr_text and not caption:
        return VisualSourceResult(
            VisualExtractionImage(
                image_id=image_id,
                source=source.source,
                source_kind=source.source_kind,
                mime_type=mime_type,
                content_sha256=source_hash,
                status="failed",
                error_type="empty_extraction",
                error=text[:1000],
                provider=endpoint.provider,
                model=endpoint.model,
                prompt_version=config.visual_extraction.prompt_version,
            ),
            retries=retries,
        )
    image = VisualExtractionImage(
        image_id=image_id,
        source=source.source,
        source_kind=source.source_kind,
        mime_type=mime_type,
        content_sha256=source_hash,
        status="success",
        ocr_text=ocr_text,
        caption=caption,
        confidence=confidence if isinstance(confidence, int | float) else None,
        provider=endpoint.provider,
        model=response.model,
        prompt_version=config.visual_extraction.prompt_version,
    )
    if image_cache is not None:
        image_cache[source_hash] = image
    return VisualSourceResult(
        image=image,
        usage=LLMUsage(
            run_id=run_id,
            dataset_name=dataset_name,
            generated_at=generated_at,
            document_id=bundle.document_id,
            page_id=bundle.metadata.page_id,
            space_key=bundle.metadata.space_key,
            source_updated_at=bundle.metadata.updated_at,
            source_content_hash=bundle.source_content_hash,
            task="visual_ocr",
            provider=endpoint.provider,
            model=response.model,
            prompt_version=config.visual_extraction.prompt_version,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            attempts=attempts,
            retries=retries,
            elapsed_ms=elapsed_ms,
        ),
        retries=retries,
    )


def build_visual_probe_endpoint(config: AppConfig) -> ProbeEndpoint:
    data = config.model_dump(mode="python")
    data["llm"]["provider"] = config.visual_extraction.provider or config.llm.provider
    data["llm"]["model"] = config.visual_extraction.model or config.llm.model
    data["llm"].setdefault("azure_openai", {})["deployment_env"] = ""
    data["llm"].setdefault("azure_ai_foundry", {})["deployment_env"] = ""
    data["llm"].setdefault("openai_compatible", {})["model_env"] = ""
    return build_probe_endpoint(AppConfig.model_validate(data))


def visual_http_error(response: httpx.Response) -> LLMError:
    retry_after: float | None = None
    retry_after_value = response.headers.get("Retry-After")
    if retry_after_value:
        try:
            retry_after = float(retry_after_value)
        except ValueError:
            retry_after = None
    retryable = response.status_code in {408, 409, 429, 500, 502, 503, 504}
    return LLMError(
        f"Provider returned HTTP {response.status_code}: {response.text[:1000]}",
        retryable=retryable,
        status_code=response.status_code,
        retry_after=retry_after,
        error_type=f"http_{response.status_code}",
    )


def load_image_bytes(source: VisualSource) -> tuple[bytes, str]:
    if source.source_kind == "data_url":
        header, encoded = source.source.split(",", 1)
        mime_type = header.split(";", 1)[0].removeprefix("data:") or "image/png"
        return base64.b64decode(encoded), mime_type
    if source.source_kind == "file":
        path = Path(source.source)
        return path.read_bytes(), source.mime_type or mime_type_for(path.name) or "image/png"
    raise ValueError("Remote image source is not available in the local cache")


def visual_source_content_sha256(source: VisualSource) -> str | None:
    if source.source_kind == "url":
        return None
    try:
        image_bytes, _mime_type = load_image_bytes(source)
    except (OSError, ValueError):
        return None
    return hashlib.sha256(image_bytes).hexdigest()


def low_value_image_reason(
    source: VisualSource, image_bytes: bytes, *, config: AppConfig
) -> str | None:
    if not config.visual_extraction.skip_low_value_images:
        return None
    name = Path(unquote(urlparse(source.source).path)).name.lower()
    if re.search(r"(^|[-_.])(logo|avatar|icon|badge|spacer|blank|placeholder)([-_.]|$)", name):
        return "filename suggests logo/icon/placeholder content"
    dimensions = image_dimensions(image_bytes)
    if dimensions is None:
        return None
    width, height = dimensions
    if width * height <= config.visual_extraction.min_image_pixels:
        return f"tiny image dimensions {width}x{height}"
    return None


def image_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        width, height = struct.unpack(">II", image_bytes[16:24])
        return int(width), int(height)
    if image_bytes.startswith((b"GIF87a", b"GIF89a")) and len(image_bytes) >= 10:
        width, height = struct.unpack("<HH", image_bytes[6:10])
        return int(width), int(height)
    if image_bytes.startswith(b"\xff\xd8"):
        return jpeg_dimensions(image_bytes)
    return None


def jpeg_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(image_bytes):
        if image_bytes[index] != 0xFF:
            index += 1
            continue
        marker = image_bytes[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(image_bytes):
            return None
        length = int.from_bytes(image_bytes[index : index + 2], "big")
        if length < 2 or index + length > len(image_bytes):
            return None
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(image_bytes[index + 3 : index + 5], "big")
            width = int.from_bytes(image_bytes[index + 5 : index + 7], "big")
            return int(width), int(height)
        index += length
    return None


def build_visual_payload(
    endpoint: ProbeEndpoint, *, image_bytes: bytes, mime_type: str, title_hint: str
) -> dict[str, Any]:
    image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    prompt = (
        "Extract source evidence from this Confluence image. Return only JSON with keys "
        '"ocr_text", "caption", and "confidence". '
        "The caption should describe UI state, diagrams, charts, or operational meaning. "
        f"Image filename hint: {title_hint or 'unknown'}."
    )
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


def parse_visual_response(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"caption": text.strip()}
    if not isinstance(parsed, dict):
        return {"caption": text.strip()}
    ocr_text = parsed.get("ocr_text") or parsed.get("image_text") or ""
    caption = parsed.get("caption") or parsed.get("description") or ""
    confidence = parsed.get("confidence")
    return {
        "ocr_text": str(ocr_text).strip(),
        "caption": str(caption).strip(),
        "confidence": confidence,
    }


def build_visual_artifact(
    *,
    bundle: PageBundle,
    run_id: str,
    dataset_name: str,
    generated_at: str,
    endpoint: ProbeEndpoint,
    prompt_version: str,
    status: Literal["complete", "partial", "failed", "skipped"],
    images: list[VisualExtractionImage],
    images_succeeded: int = 0,
    images_failed: int = 0,
    images_skipped: int = 0,
) -> VisualExtractionArtifact:
    return VisualExtractionArtifact(
        run_id=run_id,
        dataset_name=dataset_name,
        generated_at=generated_at,
        source_system="confluence",
        document_id=bundle.document_id,
        page_id=bundle.metadata.page_id,
        space_key=bundle.metadata.space_key,
        source_updated_at=bundle.metadata.updated_at,
        source_content_hash=bundle.source_content_hash,
        extracted_at=utc_now(),
        status=status,
        provider=endpoint.provider,
        model=endpoint.model,
        prompt_version=prompt_version,
        image_count=len(images),
        images_succeeded=images_succeeded,
        images_failed=images_failed,
        images_skipped=images_skipped,
        images=images,
    )


def mime_type_for(name: str) -> str | None:
    guessed = mimetypes.guess_type(name)[0]
    if guessed and guessed.startswith("image/"):
        return guessed
    suffix = Path(unquote(name)).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix[1:]}"
    return None


def local_attachment_for_url(bundle: PageBundle, source: str) -> Path | None:
    if not bundle.paths.attachments.exists():
        return None
    parsed = urlparse(source)
    filename = Path(unquote(parsed.path)).name
    if not filename:
        return None
    exact = bundle.paths.attachments / filename
    if exact.exists() and exact.is_file():
        return exact
    lowered = filename.lower()
    for path in bundle.paths.attachments.iterdir():
        if path.is_file() and path.name.lower() == lowered:
            return path
    return None


def visual_image_id(index: int, source: VisualSource) -> str:
    source_hash = hashlib.sha256(source.source.encode("utf-8")).hexdigest()[:10]
    return f"image-{index:03d}-{source_hash}"


def run_extract_visuals_for_page(
    **kwargs: Any,
) -> tuple[VisualExtractionArtifact, int, list[LLMUsage], int]:
    return asyncio.run(extract_visuals_for_page(**kwargs))
