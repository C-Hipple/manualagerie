"""Ask the LLM to describe a manual from its first pages."""
import json
import logging

from ..llm.client import get_client
from ..config import get_settings

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Descriptive title, e.g. 'Bosch 800 Series Dishwasher – Installation Instructions'"},
        "brand": {"type": "string"},
        "model_numbers": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string", "description": "Short lowercase appliance/device category, e.g. dishwasher, furnace, router, lawn mower"},
        "doc_type": {"type": "string", "enum": ["user_manual", "installation", "quick_start", "parts", "warranty", "service", "other"]},
        "device_name": {"type": "string", "description": "Friendly household name, e.g. 'Dishwasher' or 'Water heater'"},
        "summary": {"type": "string", "description": "2-3 sentences on what this document covers"},
    },
    "required": ["title", "brand", "model_numbers", "category", "doc_type", "device_name", "summary"],
    "additionalProperties": False,
}

PROMPT = """You are cataloguing a household appliance/device manual. From the filename and the opening pages below, \
extract metadata. Use empty strings / empty arrays when unknown; do not invent model numbers.

Filename: {filename}
Total pages: {page_count}

--- OPENING PAGES ---
{text}
"""


def extract_metadata(filename: str, page_texts: list[str], page_count: int) -> dict:
    s = get_settings()
    text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(page_texts))[:12000]
    resp = get_client().chat.completions.create(
        model=s.metadata_model,
        messages=[{"role": "user", "content": PROMPT.format(filename=filename, page_count=page_count, text=text)}],
        response_format={"type": "json_schema", "json_schema": {"name": "manual_metadata", "strict": True, "schema": SCHEMA}},
        temperature=0,
        max_tokens=1200,
    )
    raw = resp.choices[0].message.content or "{}"
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    out = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items()}
    if isinstance(out.get("model_numbers"), list):
        out["model_numbers"] = ", ".join(m.strip() for m in out["model_numbers"] if m and m.strip())
    return out
