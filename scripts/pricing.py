#!/usr/bin/env python3
"""Official public API pricing table & cost calculator for Antigravity models.

Prices are USD per 1,000,000 tokens (input = uncached input, cache_read = cached input hit,
output = generated + thinking tokens).

Supports:
- Date-aware introductory -> standard price transitions (e.g., Gemini 4 Carbon/Argon on 2026-12-01,
  Gemini 3.8 Flash on 2027-01-01).
- Context-length tiered pricing (>200K total prompt tokens for Gemini 3.1 Pro / 2.5 Pro).
- Separate quota pool attribution ('gemini' vs 'claude_gpt') matching Antigravity's dual pools.
- User overrides at ~/.gemini/local-usage-monitor/pricing_overrides.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DATA_DIR = Path.home() / ".gemini" / "local-usage-monitor"
OVERRIDES_PATH = DATA_DIR / "pricing_overrides.json"


@dataclass(frozen=True)
class ModelRate:
    canonical_id: str
    display_name: str
    pool: str  # "gemini" | "claude_gpt" | "other"
    input_per_m: float
    cache_read_per_m: float
    output_per_m: float
    long_context_threshold: Optional[int] = None
    long_input_per_m: Optional[float] = None
    long_cache_read_per_m: Optional[float] = None
    long_output_per_m: Optional[float] = None
    note: str = ""


# Cutoff timestamps for documented introductory pricing transitions (UTC)
_DEC_1_2026 = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
_JAN_1_2027 = datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _normalize_model_key(raw_model: str) -> str:
    """Normalize display names or IDs like 'Gemini 4 Hydrogen (XHigh)' or 'gemini-4-neon-high'."""
    if not raw_model:
        return "unknown"
    s = raw_model.strip().lower()
    # Strip parenthetical suffixes like "(xhigh)", "(high)", "(thinking)"
    s = re.sub(r"\([^)]*\)", " ", s)
    # Normalize separators to hyphens
    s = re.sub(r"[_\s/]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    # Remove thinking-level suffixes & orchestrator prefixes/suffixes for base pricing match
    s = re.sub(r"^(orchestrator-for-|orchestrator-)", "", s)
    s = re.sub(r"-(xhigh|extra|high|medium|med|low|minimal|thinking|orchestrator)$", "", s)
    # Repeat once in case of combined suffixes like "-thinking-high"
    s = re.sub(r"-(xhigh|extra|high|medium|med|low|minimal|thinking|orchestrator)$", "", s)
    return s or "unknown"


def _load_overrides() -> Dict[str, Dict[str, Any]]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        data = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def resolve_model_rate(raw_model: str, event_dt: Optional[datetime] = None) -> Tuple[Optional[ModelRate], str]:
    """Return (ModelRate or None, normalized_key) for a given raw model identifier and timestamp."""
    if event_dt is None:
        event_dt = datetime.now(timezone.utc)
    elif event_dt.tzinfo is None:
        event_dt = event_dt.replace(tzinfo=timezone.utc)

    norm = _normalize_model_key(raw_model)

    # 1. Check user overrides first (exact raw match or normalized match)
    overrides = _load_overrides()
    for candidate in (raw_model, norm):
        if candidate in overrides and isinstance(overrides[candidate], dict):
            cfg = overrides[candidate]
            try:
                rate = ModelRate(
                    canonical_id=cfg.get("canonical_id", norm),
                    display_name=cfg.get("display_name", raw_model or norm),
                    pool=cfg.get("pool", "gemini" if "gemini" in norm else "claude_gpt"),
                    input_per_m=float(cfg["input_per_m"]),
                    cache_read_per_m=float(cfg["cache_read_per_m"]),
                    output_per_m=float(cfg["output_per_m"]),
                    long_context_threshold=cfg.get("long_context_threshold"),
                    long_input_per_m=cfg.get("long_input_per_m"),
                    long_cache_read_per_m=cfg.get("long_cache_read_per_m"),
                    long_output_per_m=cfg.get("long_output_per_m"),
                    note=cfg.get("note", "custom override"),
                )
                return rate, norm
            except Exception:
                pass

    # 2. Gemini 4 Family (implicit caching = 95% off -> 0.05x)
    if "gemini-4-hydrogen" in norm or norm == "hydrogen":
        return ModelRate(
            canonical_id="gemini-4-hydrogen",
            display_name="Gemini 4 Hydrogen",
            pool="gemini",
            input_per_m=1.50,
            cache_read_per_m=0.075,
            output_per_m=7.50,
        ), "gemini-4-hydrogen"

    if "gemini-4-neon" in norm or norm == "neon":
        return ModelRate(
            canonical_id="gemini-4-neon",
            display_name="Gemini 4 Neon",
            pool="gemini",
            input_per_m=3.00,
            cache_read_per_m=0.15,
            output_per_m=15.00,
        ), "gemini-4-neon"

    if "gemini-4-carbon" in norm or norm == "carbon":
        if event_dt < _DEC_1_2026:
            return ModelRate(
                canonical_id="gemini-4-carbon",
                display_name="Gemini 4 Carbon",
                pool="gemini",
                input_per_m=2.00,
                cache_read_per_m=0.10,
                output_per_m=10.00,
                note="Intro price through 2026-11-30",
            ), "gemini-4-carbon"
        return ModelRate(
            canonical_id="gemini-4-carbon",
            display_name="Gemini 4 Carbon",
            pool="gemini",
            input_per_m=4.00,
            cache_read_per_m=0.20,
            output_per_m=20.00,
            note="Standard price from 2026-12-01",
        ), "gemini-4-carbon"

    if "gemini-4-argon" in norm or norm == "argon":
        if event_dt < _DEC_1_2026:
            return ModelRate(
                canonical_id="gemini-4-argon",
                display_name="Gemini 4 Argon",
                pool="gemini",
                input_per_m=2.00,
                cache_read_per_m=0.10,
                output_per_m=10.00,
                note="Intro price through 2026-11-30",
            ), "gemini-4-argon"
        return ModelRate(
            canonical_id="gemini-4-argon",
            display_name="Gemini 4 Argon",
            pool="gemini",
            input_per_m=4.00,
            cache_read_per_m=0.20,
            output_per_m=20.00,
            note="Standard price from 2026-12-01",
        ), "gemini-4-argon"

    # 3. Gemini 3.x / 2.5 Family
    if "gemini-3.8-flash" in norm or "gemini-3-8-flash" in norm:
        if event_dt < _JAN_1_2027:
            return ModelRate(
                canonical_id="gemini-3.8-flash",
                display_name="Gemini 3.8 Flash",
                pool="gemini",
                input_per_m=0.75,
                cache_read_per_m=0.075,
                output_per_m=3.75,
                note="Intro price through 2026-12-31",
            ), "gemini-3.8-flash"
        return ModelRate(
            canonical_id="gemini-3.8-flash",
            display_name="Gemini 3.8 Flash",
            pool="gemini",
            input_per_m=1.50,
            cache_read_per_m=0.15,
            output_per_m=7.50,
            note="Standard price from 2027-01-01",
        ), "gemini-3.8-flash"

    if any(k in norm for k in ("gemini-3.7-flash", "gemini-3-7-flash", "gemini-3.6-flash", "gemini-3-6-flash")):
        cid = "gemini-3.7-flash" if "3.7" in norm or "3-7" in norm else "gemini-3.6-flash"
        return ModelRate(
            canonical_id=cid,
            display_name=cid.replace("-", " ").title(),
            pool="gemini",
            input_per_m=1.50,
            cache_read_per_m=0.15,
            output_per_m=7.50,
        ), cid

    if "gemini-3.5-flash-lite" in norm or "gemini-3-5-flash-lite" in norm:
        return ModelRate(
            canonical_id="gemini-3.5-flash-lite",
            display_name="Gemini 3.5 Flash-Lite",
            pool="gemini",
            input_per_m=0.30,
            cache_read_per_m=0.03,
            output_per_m=2.50,
        ), "gemini-3.5-flash-lite"

    if "gemini-3.1-flash-lite" in norm or "gemini-3-1-flash-lite" in norm:
        return ModelRate(
            canonical_id="gemini-3.1-flash-lite",
            display_name="Gemini 3.1 Flash-Lite",
            pool="gemini",
            input_per_m=0.25,
            cache_read_per_m=0.025,
            output_per_m=1.50,
        ), "gemini-3.1-flash-lite"

    if "gemini-3.1-pro" in norm or "gemini-3-1-pro" in norm or "gemini-3-pro" in norm:
        return ModelRate(
            canonical_id="gemini-3.1-pro",
            display_name="Gemini 3.1 Pro",
            pool="gemini",
            input_per_m=2.00,
            cache_read_per_m=0.20,
            output_per_m=12.00,
            long_context_threshold=200_000,
            long_input_per_m=4.00,
            long_cache_read_per_m=0.40,
            long_output_per_m=18.00,
        ), "gemini-3.1-pro"

    if "gemini-2.5-pro" in norm or "gemini-2-5-pro" in norm:
        return ModelRate(
            canonical_id="gemini-2.5-pro",
            display_name="Gemini 2.5 Pro",
            pool="gemini",
            input_per_m=1.25,
            cache_read_per_m=0.125,
            output_per_m=10.00,
            long_context_threshold=200_000,
            long_input_per_m=2.50,
            long_cache_read_per_m=0.25,
            long_output_per_m=15.00,
        ), "gemini-2.5-pro"

    if "gemini-2.5-flash" in norm or "gemini-2-5-flash" in norm:
        return ModelRate(
            canonical_id="gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            pool="gemini",
            input_per_m=0.30,
            cache_read_per_m=0.03,
            output_per_m=2.50,
        ), "gemini-2.5-flash"

    # Antigravity internal Orchestrator / Worker aliases (estimated from closest public Gemini SKU;
    # users can override exact rates in ~/.gemini/local-usage-monitor/pricing_overrides.json)
    raw_lower = (raw_model or "").strip().lower()
    if "flash-worker" in raw_lower or norm in ("gemini-flash", "gemini-flash-worker-agy"):
        if event_dt < _JAN_1_2027:
            return ModelRate(
                canonical_id="gemini-flash-worker",
                display_name="Gemini Flash Worker*",
                pool="gemini",
                input_per_m=0.75,
                cache_read_per_m=0.075,
                output_per_m=3.75,
                note="Estimated using Gemini 3.8 Flash intro rate",
            ), "gemini-flash-worker"
        return ModelRate(
            canonical_id="gemini-flash-worker",
            display_name="Gemini Flash Worker*",
            pool="gemini",
            input_per_m=1.50,
            cache_read_per_m=0.15,
            output_per_m=7.50,
            note="Estimated using Gemini 3.8 Flash standard rate",
        ), "gemini-flash-worker"

    if "pro-orchestrator" in raw_lower or norm == "gemini-pro":
        return ModelRate(
            canonical_id="gemini-pro-orchestrator",
            display_name="Gemini Pro Orchestrator*",
            pool="gemini",
            input_per_m=2.00,
            cache_read_per_m=0.20,
            output_per_m=12.00,
            long_context_threshold=200_000,
            long_input_per_m=4.00,
            long_cache_read_per_m=0.40,
            long_output_per_m=18.00,
            note="Estimated using Gemini 3.1 Pro public rate",
        ), "gemini-pro-orchestrator"

    # 4. Claude Family (Claude & GPT quota pool)
    if "claude" in norm:
        if "opus" in norm and ("5.5" in norm or "5-5" in norm):
            return ModelRate(
                canonical_id="claude-opus-5.5",
                display_name="Claude Opus 5.5",
                pool="claude_gpt",
                input_per_m=4.00,
                cache_read_per_m=0.20,
                output_per_m=20.00,
            ), "claude-opus-5.5"
        if "sonnet" in norm and ("5.5" in norm or "5-5" in norm):
            return ModelRate(
                canonical_id="claude-sonnet-5.5",
                display_name="Claude Sonnet 5.5",
                pool="claude_gpt",
                input_per_m=2.00,
                cache_read_per_m=0.10,
                output_per_m=10.00,
            ), "claude-sonnet-5.5"
        if "opus" in norm and re.search(r"(^|-)5($|-)", norm):
            return ModelRate(
                canonical_id="claude-opus-5",
                display_name="Claude Opus 5",
                pool="claude_gpt",
                input_per_m=5.00,
                cache_read_per_m=0.50,
                output_per_m=25.00,
            ), "claude-opus-5"
        if "sonnet" in norm and re.search(r"(^|-)5($|-)", norm):
            return ModelRate(
                canonical_id="claude-sonnet-5",
                display_name="Claude Sonnet 5",
                pool="claude_gpt",
                input_per_m=2.00,
                cache_read_per_m=0.20,
                output_per_m=10.00,
            ), "claude-sonnet-5"
        if "opus" in norm and any(v in norm for v in ("4.6", "4-6", "4.5", "4-5")):
            ver = "4.6" if ("4.6" in norm or "4-6" in norm) else "4.5"
            return ModelRate(
                canonical_id=f"claude-opus-{ver}",
                display_name=f"Claude Opus {ver}",
                pool="claude_gpt",
                input_per_m=5.00,
                cache_read_per_m=0.50,
                output_per_m=25.00,
            ), f"claude-opus-{ver}"
        if "sonnet" in norm and any(v in norm for v in ("4.6", "4-6", "4.5", "4-5")):
            ver = "4.6" if ("4.6" in norm or "4-6" in norm) else "4.5"
            return ModelRate(
                canonical_id=f"claude-sonnet-{ver}",
                display_name=f"Claude Sonnet {ver}",
                pool="claude_gpt",
                input_per_m=3.00,
                cache_read_per_m=0.30,
                output_per_m=15.00,
            ), f"claude-sonnet-{ver}"
        if "haiku" in norm:
            return ModelRate(
                canonical_id="claude-haiku",
                display_name="Claude Haiku",
                pool="claude_gpt",
                input_per_m=1.00,
                cache_read_per_m=0.10,
                output_per_m=5.00,
            ), "claude-haiku"

    # 5. GPT-OSS Family (Claude & GPT quota pool)
    if "gpt-oss" in norm:
        return ModelRate(
            canonical_id="gpt-oss-120b",
            display_name="GPT-OSS 120B",
            pool="claude_gpt",
            input_per_m=0.15,
            cache_read_per_m=0.075,
            output_per_m=0.60,
        ), "gpt-oss-120b"

    return None, norm


def calculate_step_cost(
    raw_model: str,
    input_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    event_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Calculate official API-equivalent cost (USD) for a single PLANNER_RESPONSE step.

    Note: In Antigravity transcript.jsonl, `input_tokens` is strictly uncached fresh input tokens,
    and `cache_read_tokens` is cached prefix input tokens. Total prompt context = input_tokens + cache_read_tokens.
    """
    rate, norm = resolve_model_rate(raw_model, event_dt)
    total_prompt = max(0, int(input_tokens)) + max(0, int(cache_read_tokens))
    out_tok = max(0, int(output_tokens))
    in_tok = max(0, int(input_tokens))
    cache_tok = max(0, int(cache_read_tokens))

    if rate is None:
        pool = "gemini" if "gemini" in norm else ("claude_gpt" if ("claude" in norm or "gpt" in norm) else "other")
        return {
            "matched": False,
            "canonical_id": norm,
            "display_name": raw_model or norm or "Unknown",
            "pool": pool,
            "input_cost": 0.0,
            "cache_read_cost": 0.0,
            "output_cost": 0.0,
            "total_cost": 0.0,
            "cache_savings": 0.0,
            "tier": "unpriced",
        }

    is_long = (
        rate.long_context_threshold is not None
        and total_prompt > rate.long_context_threshold
        and rate.long_input_per_m is not None
        and rate.long_cache_read_per_m is not None
        and rate.long_output_per_m is not None
    )

    in_rate = rate.long_input_per_m if is_long else rate.input_per_m
    cache_rate = rate.long_cache_read_per_m if is_long else rate.cache_read_per_m
    out_rate = rate.long_output_per_m if is_long else rate.output_per_m

    input_cost = (in_tok / 1_000_000.0) * in_rate
    cache_read_cost = (cache_tok / 1_000_000.0) * cache_rate
    output_cost = (out_tok / 1_000_000.0) * out_rate
    total_cost = input_cost + cache_read_cost + output_cost

    # How much caching saved vs paying full input rate for those cached tokens
    cache_savings = (cache_tok / 1_000_000.0) * max(0.0, in_rate - cache_rate)

    return {
        "matched": True,
        "canonical_id": rate.canonical_id,
        "display_name": rate.display_name,
        "pool": rate.pool,
        "input_cost": input_cost,
        "cache_read_cost": cache_read_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
        "cache_savings": cache_savings,
        "tier": ">200k" if is_long else "standard",
    }
