"""Optional AI helper: one validated personal opening line per person (the ``{hook}`` template variable) and
plain-English audience suggestions, through any OpenAI-compatible endpoint (free tiers and local models included).

Connecto works fully without it. Every method used while sending returns None on failure instead of raising.
"""
from __future__ import annotations

from .assist import Assistant
from .client import AIClient, AIError, TestResult
from .guard import clean_hook, facts_text, parse_audience
from .presets import PRESETS, Preset, choose_model, get_preset, rank_models
from .prompts import PROMPT_VERSION

__all__ = ["Assistant", "AIClient", "AIError", "TestResult", "Preset", "PRESETS", "get_preset", "rank_models",
           "choose_model", "clean_hook", "facts_text", "parse_audience", "PROMPT_VERSION"]
