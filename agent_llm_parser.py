#!/usr/bin/env python3
"""Text enrichment agent — fills name_en, program, material, atmosphere.

Takes a single building dict from 1_buildings_raw.json and calls the
Anthropic API, using tool-use (forced) to return structured enrichment data
with vocab-enforced enum constraints baked into the input_schema.

No retry logic here — the caller (pipeline_harness) handles retries.

Raises:
    anthropic.APIError subclasses — API-level failure (rate limit, server error)
    ValueError — vocabulary violation or malformed tool output
"""

import hashlib
import json
import os

from dataclasses import dataclass
from dotenv import load_dotenv

import vocab

# anthropic is imported lazily inside enrich_building() so that this module's
# prompt_version() and schema helpers can be used in environments without the
# Anthropic SDK installed (e.g. for offline testing or the tasks_db worker
# orchestration layer).

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

_SYSTEM_PROMPT = """\
You are an architecture database curator. Call the record_enrichment tool
to record structured metadata for the building in the user message.

Rules:
- name_en: The building/project name ONLY. Strip editorial hooks like
  "Restoring the essence." or "Play boxes." — keep only the actual project name.
  Must be 3+ characters.
- program: Choose the PRIMARY use from the enum.
- material: Primary construction materials (free-form string). Omit if unclear.
- atmosphere: Choose the dominant feeling from the enum.

Do not emit prose. Only call the tool.
"""

_FEW_SHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data", "few_shot", "enrich_examples.json",
)


def _load_few_shot() -> list:
    """Load optional few-shot examples. Each entry: {input: str, output: dict}.
    If the file is missing or empty, returns []. Any edits to this file bump
    the prompt_version automatically (see prompt_version() — it hashes the
    examples list)."""
    if not os.path.exists(_FEW_SHOT_PATH):
        return []
    try:
        with open(_FEW_SHOT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def prompt_version() -> str:
    """Stable version tag for the current system prompt + tool schema + few-shot examples.

    Format: `enrich-{sha256(...)[:8]}`. Auto-bumps on any edit to _SYSTEM_PROMPT,
    the tool schema (including vocab changes), or `data/few_shot/enrich_examples.json`.
    The tasks ledger treats a prompt change as a new key — no stale cache hits.
    """
    payload = (
        _SYSTEM_PROMPT
        + json.dumps(_enrichment_tool(), sort_keys=True)
        + json.dumps(_load_few_shot(), sort_keys=True)
    )
    return f"enrich-{hashlib.sha256(payload.encode()).hexdigest()[:8]}"


def _enrichment_tool() -> dict:
    """Tool schema with vocab enums — regenerated on every call so it stays
    in lock-step with vocab.py, even if vocabularies change at runtime."""
    return {
        "name": "record_enrichment",
        "description": "Record the enriched architecture metadata for this building.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name_en": {
                    "type": "string",
                    "minLength": 3,
                    "description": "Canonical English building/project name. Strip editorial hooks.",
                },
                "program": {
                    "type": "string",
                    "enum": sorted(vocab.PROGRAM),
                    "description": "Primary building use.",
                },
                "material": {
                    "type": "string",
                    "description": "Primary construction materials. Omit when unclear.",
                },
                "atmosphere": {
                    "type": "string",
                    "enum": sorted(vocab.ATMOSPHERE),
                    "description": "Dominant atmosphere/feeling.",
                },
            },
            "required": ["name_en", "program", "atmosphere"],
        },
    }


@dataclass
class EnrichmentResult:
    building_id: str
    name_en: str
    program: str
    material: str
    atmosphere: str


def _build_prompt(building: dict) -> str:
    tags = ", ".join(building.get("tags") or [])
    description = (building.get("description") or "")[:800]

    return f"""\
Building data to enrich:

project_name: {building.get("project_name") or ""}
architect: {building.get("architect") or ""}
location: {building.get("city") or ""}, {building.get("location_country") or ""}
year: {building.get("year") or ""}
building_type: {building.get("building_type") or ""}
material (raw): {building.get("material") or ""}
description: {description}
tags: {tags}

Call the record_enrichment tool with the extracted metadata."""


def _extract_tool_input(message) -> dict:
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_enrichment":
            return block.input
    raise ValueError("model did not call record_enrichment tool")


def _parse_response(data: dict, building_id: str) -> EnrichmentResult:
    name_en = (data.get("name_en") or "").strip()
    if len(name_en) < 3:
        raise ValueError(f"name_en too short or missing: {name_en!r}")

    program = (data.get("program") or "").strip()
    if not vocab.is_valid("program", program):
        raise ValueError(f"program {program!r} not in valid vocabulary")

    atmosphere = (data.get("atmosphere") or "").strip()
    if not vocab.is_valid("atmosphere", atmosphere):
        raise ValueError(f"atmosphere {atmosphere!r} not in valid vocabulary")

    material = data.get("material")
    if material is not None:
        material = str(material).strip() or None

    return EnrichmentResult(
        building_id=building_id,
        name_en=name_en,
        program=program,
        material=material,
        atmosphere=atmosphere,
    )


def enrich_building(building: dict) -> EnrichmentResult:
    """Call Anthropic API to enrich a single raw building.

    Uses forced tool_use with a vocab-derived input_schema so the model is
    structurally constrained to produce valid enum values.

    Args:
        building: A building dict from 1_buildings_raw.json. Must have building_id.

    Returns:
        EnrichmentResult with name_en, program, material, atmosphere.

    Raises:
        anthropic.APIError: API-level failure (rate limit, server error, auth).
        ValueError: Malformed tool output or residual vocabulary violation.
    """
    import anthropic
    import config

    building_id = building["building_id"]
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    tool = _enrichment_tool()

    # Few-shot examples (empty by default). Each example is prepended to the
    # conversation as a user/assistant turn — a simple in-context pattern
    # that costs a few hundred tokens per call if populated.
    messages = []
    for ex in _load_few_shot():
        if not isinstance(ex, dict):
            continue
        user_input = ex.get("input")
        tool_output = ex.get("output")
        if not user_input or not isinstance(tool_output, dict):
            continue
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"example_{len(messages)}",
             "name": tool["name"], "input": tool_output},
        ]})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"example_{len(messages) - 1}",
             "content": "noted"},
        ]})
    messages.append({"role": "user", "content": _build_prompt(building)})

    # Prompt caching: system prompt + tool schema are identical across calls,
    # so mark the last tool with cache_control. The API caches everything up
    # through the marked block (system + tool), yielding large input-token
    # savings on the N-call batch processing pattern.
    cached_tool = {**tool, "cache_control": {"type": "ephemeral"}}
    cached_system = [{"type": "text", "text": _SYSTEM_PROMPT,
                      "cache_control": {"type": "ephemeral"}}]

    message = client.messages.create(
        model=config.HARNESS_MODEL,
        max_tokens=512,
        system=cached_system,
        tools=[cached_tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=messages,
    )

    data = _extract_tool_input(message)
    return _parse_response(data, building_id)
