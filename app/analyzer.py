"""Call Claude to analyse and convert one Classic customisation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import anthropic

from .parser import Customisation

RULES = (Path(__file__).parent / "rules" / "mithilai_rules.md").read_text(encoding="utf-8")
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "high")
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "48000"))

APPROACHES = [
    "AppStudioLayer", "BPM", "EpicorFunction", "BAQ", "Report",
    "DropStandard", "DropUnused", "Rebuild", "NeedsReview",
]
BUCKETS = ["Drop", "Layer", "BPM/Function", "Layer + BPM/Function", "Rebuild", "Needs Review"]
FILE_TYPES = ["bpm_code", "function_code", "layer_json", "layer_steps", "baq_design", "design_doc", "code_skeleton", "other"]


def _obj(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}

ANALYSIS_SCHEMA = _obj({
    "what_it_does": {"type": "array", "items": _STR,
                     "description": "Plain-English list of behaviours, one per item."},
    "parts": {"type": "array", "items": _obj({
        "behaviour": _STR,
        "classic_mechanism": _STR,
        "kinetic_approach": {"type": "string", "enum": APPROACHES},
        "reason": _STR,
    })},
    "bucket": {"type": "string", "enum": BUCKETS},
    "confidence": {"type": "integer", "description": "0-100"},
    "confidence_reason": _STR,
    "hours_build": {"type": "number"},
    "hours_test": {"type": "number"},
    "hours_deploy": {"type": "number"},
    "estimate_notes": _STR,
    "improvements": _STR_LIST,
    "risks": _STR_LIST,
    "questions_for_client": _STR_LIST,
    "files": {"type": "array", "items": _obj({
        "path": _STR,
        "file_type": {"type": "string", "enum": FILE_TYPES},
        "description": _STR,
        "content": _STR,
    })},
    "import_steps": _STR_LIST,
    "test_cases": {"type": "array", "items": _obj({
        "test": _STR,
        "expected": _STR,
        "channel": {"type": "string", "description": "Screen, DMT, REST, EDI, etc."},
    })},
})


class AnalysisError(RuntimeError):
    pass


# USD per 1M tokens (input, output) from Anthropic's published pricing – check
# https://www.anthropic.com/pricing when prices change. Cache writes bill at 1.25x input,
# cache reads at 0.1x input.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost(usage: dict) -> float | None:
    """Estimated USD cost of one request, or None for an unknown model.

    A server-side fallback can report a different model than the one configured; if that
    model has no listed price, estimate at the configured model's price.
    """
    rates = PRICES.get(usage.get("model", "")) or PRICES.get(MODEL)
    if not rates:
        return None
    inp, out = rates
    return (usage["input_tokens"] * inp + usage["output_tokens"] * out
            + usage["cache_write_tokens"] * inp * 1.25 + usage["cache_read_tokens"] * inp * 0.1) / 1e6


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def content_key(item: Customisation) -> str:
    """Same screen, Classic code and UI changes → same key, whatever the customisation is called."""
    parts = (_normalise(item.form).lower(), _normalise(item.script), _normalise(item.ui_context))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def request_key(item: Customisation, system_prompt: str) -> str:
    """Fingerprint of the exact request, so an identical request can reuse a stored result."""
    fallbacks = os.getenv("CLAUDE_FALLBACKS", "on")
    payload = json.dumps([MODEL, EFFORT, fallbacks, ANALYSIS_SCHEMA, system_prompt, _user_message(item)],
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_system_prompt(profile: dict, samples: list[tuple[str, str]]) -> str:
    """Stable per-job system prompt (rules + client profile + Kinetic format samples)."""
    lines = [RULES, "\n## Client profile\n"]
    for key, value in profile.items():
        if value:
            lines.append(f"- **{key}**: {value}")
    if samples:
        lines.append("\n## Kinetic reference exports from this client's environment\n"
                     "Follow these formats exactly when generating import files.\n")
        for name, text in samples:
            lines.append(f"<kinetic_sample name=\"{name}\">\n{text}\n</kinetic_sample>")
    else:
        lines.append("\n## Kinetic reference exports\nNone provided. Follow rule 4: produce "
                     "LAYER_STEPS.md instead of layer JSON.")
    return "\n".join(lines)


def _user_message(item: Customisation) -> str:
    meta = "\n".join(f"- {k}: {v}" for k, v in item.metadata.items()) or "- (none found)"
    return (
        f"Assess and convert this Classic customisation.\n\n"
        f"Name: {item.name}\nForm: {item.form or 'unknown'}\nSource file: {item.source_file}\n"
        f"Export metadata:\n{meta}\n\n"
        f"<classic_script language=\"csharp\">\n{item.script or '(no script found)'}\n</classic_script>\n\n"
        f"<classic_ui_xml>\n{item.ui_context or '(none)'}\n</classic_ui_xml>"
    )


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)


def analyse(item: Customisation, system_prompt: str) -> tuple[dict, dict]:
    """Return (analysis dict, usage dict) for one customisation."""
    kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _user_message(item)}],
        thinking={"type": "adaptive"},
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
    )
    # Server-side refusal fallback (supported on the Opus 5 / Fable families).
    if MODEL.startswith(("claude-opus-5", "claude-fable-5")) and os.getenv("CLAUDE_FALLBACKS", "on") == "on":
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    try:
        with _client().beta.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise AnalysisError("Anthropic API key is invalid (check ANTHROPIC_API_KEY).") from exc
    except TypeError as exc:
        if "authentication" not in str(exc).lower():
            raise
        raise AnalysisError("Anthropic API key is not set (add ANTHROPIC_API_KEY to the environment).") from exc
    except anthropic.RateLimitError as exc:
        raise AnalysisError("Rate limited by the Anthropic API after retries; try again later.") from exc
    except anthropic.BadRequestError as exc:
        raise AnalysisError(f"API rejected the request: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AnalysisError(f"API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalysisError("Could not reach the Anthropic API (network error).") from exc

    if message.stop_reason == "refusal":
        raise AnalysisError("The model declined to analyse this customisation.")
    if message.stop_reason == "max_tokens":
        raise AnalysisError("Output hit the token limit; raise CLAUDE_MAX_TOKENS or split the customisation.")

    text = next((b.text for b in message.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalysisError("Model returned invalid JSON.") from exc

    u = message.usage
    usage = {
        "model": message.model,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
    return result, usage
