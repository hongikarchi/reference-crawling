#!/usr/bin/env python3
"""Image analysis agent — fills style, color_tone, material_visual, visual_description.

Takes a single building dict from 2_buildings_enriched.json, reads its images
from disk, and calls the Anthropic vision API with forced tool_use so the
output is structurally constrained by a vocab-derived input_schema.

No retry logic here — the caller (pipeline_harness) handles retries.

Raises:
    ValueError("no_images") — no loadable images on disk (quarantine immediately)
    anthropic.APIError subclasses — API-level failure
    ValueError — vocabulary violation or malformed tool output
"""

import base64
import hashlib
import json
import os

from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from core import config
from core import vocab

# anthropic is imported lazily inside analyze_building() — see agent_llm_parser.

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB per image

_SYSTEM_PROMPT = """\
You are an architecture image analyst. Analyze the provided building photos
and call the record_image_analysis tool with your structured findings.

Rules:
- style: Pick the best-fitting architectural style from the enum.
- color_tone: Dominant color tone of the building's exterior surfaces.
- material_visual: Array of short lowercase material terms visible in the
  images (e.g., "concrete", "glass", "timber"). Empty array if unclear.
- visual_description: 1-3 sentences describing visual character, materials,
  and spatial quality. Minimum 20 characters.
- atmosphere: Include only when requested (i.e., the tool schema asks for it).

Do not emit prose. Only call the tool.
"""


def prompt_version(include_atmosphere: bool) -> str:
    """Stable version tag for the current system prompt + tool schema.

    Two distinct labels — `analyze-with-atm` and `analyze-no-atm` — because the
    two branches are genuinely different prompts (different required fields,
    different tool schema). Eval harness (Phase 4) scores them independently.
    """
    label = "analyze-with-atm" if include_atmosphere else "analyze-no-atm"
    payload = _SYSTEM_PROMPT + json.dumps(_image_analysis_tool(include_atmosphere), sort_keys=True)
    return f"{label}-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def _image_analysis_tool(include_atmosphere: bool) -> dict:
    """Tool schema with vocab enums, regenerated per call so it tracks vocab.py."""
    properties = {
        "style": {
            "type": "string",
            "enum": sorted(vocab.STYLE),
            "description": "Architectural style.",
        },
        "color_tone": {
            "type": "string",
            "enum": sorted(vocab.COLOR_TONE),
            "description": "Dominant color tone of the building's exterior.",
        },
        "material_visual": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Short lowercase material terms visible in the images.",
        },
        "visual_description": {
            "type": "string",
            "minLength": 20,
            "description": "1-3 sentences of prose describing visual character.",
        },
    }
    required = ["style", "color_tone", "material_visual", "visual_description"]
    if include_atmosphere:
        properties["atmosphere"] = {
            "type": "string",
            "enum": sorted(vocab.ATMOSPHERE),
            "description": "Dominant atmosphere/feeling.",
        }
        required.append("atmosphere")
    return {
        "name": "record_image_analysis",
        "description": "Record structured visual analysis for this building.",
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


@dataclass
class ImageAnalysisResult:
    building_id: str
    style: str
    color_tone: str
    material_visual: list = field(default_factory=list)
    visual_description: str = ""
    atmosphere: str = None


def _get_media_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _MEDIA_TYPES.get(ext, "image/jpeg")


def _fetch_url_b64(url: str) -> tuple:
    """HTTP-fetch an image URL → (base64_data, media_type). None on failure
    or oversize. Used for Phase 11 URL-only buildings (no on-disk image)."""
    import requests
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [warn] Image URL fetch failed: {url} — {e}")
        return None, None
    if len(resp.content) > _MAX_IMAGE_BYTES:
        print(f"    [warn] Image URL too large ({len(resp.content) // 1024}KB): {url}")
        return None, None
    media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
    if not media_type.startswith("image/"):
        media_type = "image/jpeg"
    return base64.standard_b64encode(resp.content).decode("utf-8"), media_type


def _load_images_b64(building: dict) -> list:
    """Up to HARNESS_MAX_IMAGES photos as base64 blocks for the Anthropic API.

    Two paths:
      • Legacy on-disk (existing 3,465 metalocus rows): read from
        images/{building_id}/{filename}, gated by 5 MB cap.
      • Phase 11 URL-only (new metalocus rows from batch 8 onward, plus
        Architizer/Archello when their image_analysis runs): HTTP-fetch
        cover_image_url + first N-1 of gallery_image_urls.

    Selection order on disk path: cover (order=0) first, then upload=True
    photos by order. URL path: cover_image_url, then gallery_image_urls in
    the order they were stored.
    """
    blocks: list = []

    # Legacy disk path
    building_id = building.get("building_id")
    if building_id:
        img_dir = os.path.join(config.IMAGE_BASE_DIR, building_id)
        images = building.get("images") or []
        photos = sorted(
            [img for img in images if img.get("type") == "photo"],
            key=lambda x: x.get("order", 999),
        )
        upload_photos = [img for img in photos if img.get("upload")]
        candidates = upload_photos if upload_photos else photos

        for img in candidates[: config.HARNESS_MAX_IMAGES]:
            filepath = os.path.join(img_dir, img["filename"])
            if not os.path.exists(filepath):
                continue
            size = os.path.getsize(filepath)
            if size > _MAX_IMAGE_BYTES:
                print(f"    [warn] Skipping large image ({size // 1024}KB): {img['filename']}")
                continue
            with open(filepath, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _get_media_type(img["filename"]),
                    "data": data,
                },
            })
            if len(blocks) >= config.HARNESS_MAX_IMAGES:
                break

    if blocks:
        return blocks

    # Phase 11 URL-only path
    urls: list = []
    if building.get("cover_image_url"):
        urls.append(building["cover_image_url"])
    for u in (building.get("gallery_image_urls") or []):
        urls.append(u)
        if len(urls) >= config.HARNESS_MAX_IMAGES:
            break

    for url in urls[: config.HARNESS_MAX_IMAGES]:
        data, media_type = _fetch_url_b64(url)
        if data is None:
            continue
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": data,
            },
        })
        if len(blocks) >= config.HARNESS_MAX_IMAGES:
            break

    return blocks


def _build_prompt(building: dict) -> str:
    return f"""\
Analyze the architecture photos above for this building:
Name: {building.get("name_en") or building.get("project_name") or ""}
Location: {building.get("city") or ""}, {building.get("location_country") or ""}
Program: {building.get("program") or ""}

Call the record_image_analysis tool with the structured visual metadata."""


def _extract_tool_input(message) -> dict:
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_image_analysis":
            return block.input
    raise ValueError("model did not call record_image_analysis tool")


def _parse_response(
    data: dict, building_id: str, include_atmosphere: bool
) -> ImageAnalysisResult:
    style = (data.get("style") or "").strip()
    if not vocab.is_valid("style", style):
        raise ValueError(f"style {style!r} not in valid vocabulary")

    color_tone = (data.get("color_tone") or "").strip()
    if not vocab.is_valid("color_tone", color_tone):
        raise ValueError(f"color_tone {color_tone!r} not in valid vocabulary")

    visual_description = (data.get("visual_description") or "").strip()
    if len(visual_description) < 20:
        raise ValueError(f"visual_description too short: {visual_description!r}")

    material_visual = data.get("material_visual")
    if not isinstance(material_visual, list):
        material_visual = []
    material_visual = [str(m).strip().lower() for m in material_visual if m]

    atmosphere = None
    if include_atmosphere:
        atmosphere = (data.get("atmosphere") or "").strip()
        if atmosphere and not vocab.is_valid("atmosphere", atmosphere):
            raise ValueError(f"atmosphere {atmosphere!r} not in valid vocabulary")
        atmosphere = atmosphere or None

    return ImageAnalysisResult(
        building_id=building_id,
        style=style,
        color_tone=color_tone,
        material_visual=material_visual,
        visual_description=visual_description,
        atmosphere=atmosphere,
    )


def analyze_building(building: dict) -> ImageAnalysisResult:
    """Call Anthropic vision API to analyze a single building's images.

    Uses forced tool_use with a vocab-derived input_schema so enum-valued
    fields are structurally constrained.

    Args:
        building: A building dict from 2_buildings_enriched.json. Must have
                  building_id and images list.

    Returns:
        ImageAnalysisResult with style, color_tone, material_visual,
        visual_description, and optionally atmosphere.

    Raises:
        ValueError("no_images"): No loadable images — quarantine immediately.
        anthropic.APIError: API-level failure.
        ValueError: Malformed tool output or residual vocab violation.
    """
    import anthropic

    building_id = building["building_id"]
    include_atmosphere = not bool(building.get("atmosphere"))

    image_blocks = _load_images_b64(building)
    if not image_blocks:
        raise ValueError("no_images")

    prompt_text = _build_prompt(building)
    content = image_blocks + [{"type": "text", "text": prompt_text}]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    tool = _image_analysis_tool(include_atmosphere)

    # Prompt caching: system prompt + tool schema are identical across calls
    # of the same atmosphere-inclusion branch. Image content is per-call and
    # therefore not cached.
    cached_tool = {**tool, "cache_control": {"type": "ephemeral"}}
    cached_system = [{"type": "text", "text": _SYSTEM_PROMPT,
                      "cache_control": {"type": "ephemeral"}}]

    message = client.messages.create(
        model=config.HARNESS_MODEL,
        max_tokens=1024,
        system=cached_system,
        tools=[cached_tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": content}],
    )

    data = _extract_tool_input(message)
    return _parse_response(data, building_id, include_atmosphere)
