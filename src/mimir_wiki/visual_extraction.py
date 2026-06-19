from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx

from mimir_wiki.cache_reader import PageBundle
from mimir_wiki.config import AppConfig
from mimir_wiki.llm.probe import ProbeEndpoint, build_probe_endpoint, extract_response_text
from mimir_wiki.schemas import VisualExtractionArtifact, VisualExtractionImage
from mimir_wiki.utils import atomic_write_json, load_json, utc_now

IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


@dataclass(frozen=True)
class VisualSource:
    source: str
    source_kind: Literal["data_url", "file", "url"]
    mime_type: str | None = None


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
    for match in MARKDOWN_IMAGE_RE.finditer(bundle.clean_markdown):
        source = match.group(1).strip("<>")
        if should_skip_image_source(source) or source in seen:
            continue
        visual_source = source_from_reference(bundle, source)
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
            seen.add(source)
            sources.append(
                VisualSource(source=source, source_kind="file", mime_type=mime_type_for(path.name))
            )
            if max_images is not None and len(sources) >= max_images:
                return sources
    return sources


def should_skip_image_source(source: str) -> bool:
    if not source or source.startswith("$"):
        return True
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "data", "file"}:
        return False
    return Path(unquote(source)).suffix.lower() not in IMAGE_EXTENSIONS


def source_from_reference(bundle: PageBundle, source: str) -> VisualSource:
    parsed = urlparse(source)
    if parsed.scheme == "data":
        mime_type = source.split(";", 1)[0].removeprefix("data:") or None
        return VisualSource(source=source, source_kind="data_url", mime_type=mime_type)
    if parsed.scheme in {"http", "https"}:
        local_path = local_attachment_for_url(bundle, source)
        if local_path is not None:
            return VisualSource(
                source=str(local_path), source_kind="file", mime_type=mime_type_for(local_path.name)
            )
        return VisualSource(source=source, source_kind="url", mime_type=mime_type_for(parsed.path))
    path = Path(unquote(source))
    if not path.is_absolute():
        path = bundle.paths.clean_md.parent / path
    return VisualSource(source=str(path), source_kind="file", mime_type=mime_type_for(path.name))


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
) -> tuple[VisualExtractionArtifact, int]:
    endpoint = build_visual_probe_endpoint(config)
    sources = discover_visual_sources(
        bundle, max_images=config.visual_extraction.max_images_per_page
    )
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
            return artifact, 1
        return artifact, 0

    images: list[VisualExtractionImage] = []
    async with httpx.AsyncClient(
        timeout=config.llm.timeout_seconds, transport=llm_transport
    ) as llm_client:
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
            image = await extract_visual_source(
                index=index,
                source=source,
                config=config,
                endpoint=endpoint,
                llm_client=llm_client,
            )
            images.append(image)
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
        return artifact, 1
    return artifact, 0


async def extract_visual_source(
    *,
    index: int,
    source: VisualSource,
    config: AppConfig,
    endpoint: ProbeEndpoint,
    llm_client: httpx.AsyncClient,
) -> VisualExtractionImage:
    image_id = visual_image_id(index, source)
    if source.source_kind == "url":
        return VisualExtractionImage(
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
        )
    try:
        image_bytes, mime_type = load_image_bytes(source)
    except (OSError, ValueError) as exc:
        return VisualExtractionImage(
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
    payload = build_visual_payload(
        endpoint,
        image_bytes=image_bytes,
        mime_type=mime_type,
        title_hint=Path(unquote(urlparse(source.source).path)).name,
    )
    try:
        response = await llm_client.post(endpoint.url, headers=endpoint.headers, json=payload)
    except httpx.HTTPError as exc:
        return VisualExtractionImage(
            image_id=image_id,
            source=source.source,
            source_kind=source.source_kind,
            mime_type=mime_type,
            content_sha256=hashlib.sha256(image_bytes).hexdigest(),
            status="failed",
            error_type=type(exc).__name__,
            error=str(exc)[:1000],
            provider=endpoint.provider,
            model=endpoint.model,
            prompt_version=config.visual_extraction.prompt_version,
        )
    if response.status_code >= 400:
        return VisualExtractionImage(
            image_id=image_id,
            source=source.source,
            source_kind=source.source_kind,
            mime_type=mime_type,
            content_sha256=hashlib.sha256(image_bytes).hexdigest(),
            status="failed",
            error_type=f"http_{response.status_code}",
            error=response.text[:1000],
            provider=endpoint.provider,
            model=endpoint.model,
            prompt_version=config.visual_extraction.prompt_version,
        )
    text = extract_response_text(response.json())
    parsed = parse_visual_response(text)
    ocr_text = parsed.get("ocr_text", "")
    caption = parsed.get("caption", "")
    confidence = parsed.get("confidence")
    if not ocr_text and not caption:
        return VisualExtractionImage(
            image_id=image_id,
            source=source.source,
            source_kind=source.source_kind,
            mime_type=mime_type,
            content_sha256=hashlib.sha256(image_bytes).hexdigest(),
            status="failed",
            error_type="empty_extraction",
            error=text[:1000],
            provider=endpoint.provider,
            model=endpoint.model,
            prompt_version=config.visual_extraction.prompt_version,
        )
    return VisualExtractionImage(
        image_id=image_id,
        source=source.source,
        source_kind=source.source_kind,
        mime_type=mime_type,
        content_sha256=hashlib.sha256(image_bytes).hexdigest(),
        status="success",
        ocr_text=ocr_text,
        caption=caption,
        confidence=confidence if isinstance(confidence, int | float) else None,
        provider=endpoint.provider,
        model=endpoint.model,
        prompt_version=config.visual_extraction.prompt_version,
    )


def build_visual_probe_endpoint(config: AppConfig) -> ProbeEndpoint:
    data = config.model_dump(mode="python")
    data["llm"]["provider"] = config.visual_extraction.provider or config.llm.provider
    data["llm"]["model"] = config.visual_extraction.model or config.llm.model
    data["llm"].setdefault("azure_openai", {})["deployment_env"] = ""
    data["llm"].setdefault("azure_ai_foundry", {})["deployment_env"] = ""
    data["llm"].setdefault("openai_compatible", {})["model_env"] = ""
    return build_probe_endpoint(AppConfig.model_validate(data))


def load_image_bytes(source: VisualSource) -> tuple[bytes, str]:
    if source.source_kind == "data_url":
        header, encoded = source.source.split(",", 1)
        mime_type = header.split(";", 1)[0].removeprefix("data:") or "image/png"
        return base64.b64decode(encoded), mime_type
    if source.source_kind == "file":
        path = Path(source.source)
        return path.read_bytes(), source.mime_type or mime_type_for(path.name) or "image/png"
    raise ValueError("Remote image source is not available in the local cache")


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


def run_extract_visuals_for_page(**kwargs: Any) -> tuple[VisualExtractionArtifact, int]:
    return asyncio.run(extract_visuals_for_page(**kwargs))
