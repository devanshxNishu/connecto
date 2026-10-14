"""Provider presets and model discovery for the optional AI helper.

Every preset speaks the OpenAI-compatible ``/chat/completions`` dialect, so one small client covers them all. Model
names churn constantly, so nothing here depends on a particular id: ``rank_models`` sorts whatever the provider's
``GET /models`` returns using *hints* (substrings in order of preference), and ``fallback_models`` is only a last
resort for when the listing itself fails.

The free-tier numbers in the notes are hedged on purpose: providers change them without warning.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Ids that are not chat models (embeddings, speech, image/video generation, moderation, live/realtime ...).
_NON_CHAT_SUBSTRINGS = (
    "embedding", "embed", "whisper", "guard", "moderation", "safeguard", "image", "imagen", "rerank", "audio",
    "realtime", "transcribe", "vision-only", "-live", "robotics", "computer-use", "lyria", "text-to-speech",
)
# Short markers are matched as whole tokens only ("tts" must not hit an unrelated id that merely contains it).
_NON_CHAT_TOKENS = frozenset({"tts", "veo", "aqa", "dall", "ocr", "bge", "minilm", "live"})
_UNSTABLE_TOKENS = frozenset({"preview", "exp", "experimental", "beta", "alpha"})
_TOPIC_PENALTY = ("coder", "-vl", "vision", "math")      # specialised models: fine as a last resort for prose
_LOCAL_MAX_B = 14.0                                      # do not auto-pick a model too big for a laptop

_TOKEN_SPLIT = re.compile(r"[^a-z0-9.]+")
_VERSION_TOKEN = re.compile(r"^v?[a-z]{0,6}?(\d{1,2}(?:\.\d{1,2}){0,2})$")
_SIZE_B = re.compile(r"(?<![a-z0-9.])(\d+(?:\.\d+)?)b(?![a-z0-9])")


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    base_url: str
    key_env: str
    signup_url: str
    needs_key: bool
    local: bool
    free_note: str            # one hedged line on the free tier
    privacy_note: str         # one line on data use
    model_hints: tuple[str, ...]       # substrings in preference order
    fallback_models: tuple[str, ...]   # used only if /models cannot be listed
    rpm: int                  # polite client-side requests per minute (0 = no pacing)
    id_must_contain: tuple[str, ...] = ()   # when set, only ids containing one of these are candidates


PRESETS: dict[str, Preset] = {
    "gemini": Preset(
        id="gemini", label="Google Gemini (AI Studio)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        key_env="GEMINI_API_KEY", signup_url="https://aistudio.google.com/apikey",
        needs_key=True, local=False,
        free_note="Free tier with a generous quota on the Flash models at the time of writing - check Google's "
                  "pricing page, limits change.",
        privacy_note="On the free tier Google may use your prompts and the replies (here: a few public profile "
                     "facts and your note) to improve its products.",
        model_hints=("flash-lite", "flash"), fallback_models=("gemini-flash-lite-latest", "gemini-flash-latest"),
        rpm=10, id_must_contain=("gemini",)),
    "groq": Preset(
        id="groq", label="Groq",
        base_url="https://api.groq.com/openai/v1", key_env="GROQ_API_KEY",
        signup_url="https://console.groq.com/keys", needs_key=True, local=False,
        free_note="Free tier with rate limits (around 30 requests a minute and roughly 1,000 a day on small models "
                  "at the time of writing); the model list changes often - check the provider's page.",
        privacy_note="Prompts are processed on Groq's servers; see Groq's privacy policy for how they are retained.",
        model_hints=("gpt-oss-20b", "gpt-oss-120b", "qwen", "llama-3.1-8b", "llama"),
        fallback_models=("openai/gpt-oss-20b", "llama-3.1-8b-instant"), rpm=20),
    "openrouter": Preset(
        id="openrouter", label="OpenRouter (free models)",
        base_url="https://openrouter.ai/api/v1", key_env="OPENROUTER_API_KEY",
        signup_url="https://openrouter.ai/keys", needs_key=True, local=False,
        free_note="Models whose id ends in ':free' cost nothing: roughly 20 requests a minute and 50 a day without "
                  "credits (about 1,000 a day once you have bought $10 or more) at the time of writing - check the "
                  "provider's page.",
        privacy_note="Providers behind free models may log prompts; send nothing you would mind them keeping.",
        model_hints=("gpt-oss", "gemma", "llama", "qwen", "mistral", "deepseek"), fallback_models=(), rpm=15),
    "ollama": Preset(
        id="ollama", label="Ollama (runs on your computer)",
        base_url="http://localhost:11434/v1", key_env="", signup_url="https://ollama.com/download",
        needs_key=False, local=True,
        free_note="Free and unlimited; a small model needs roughly 4-8 GB of free RAM and is slower on older "
                  "machines.",
        privacy_note="Runs entirely on your machine: nothing leaves it.",
        model_hints=("qwen", "llama", "gemma", "mistral", "phi"), fallback_models=(), rpm=120),
    "custom": Preset(
        id="custom", label="Custom (any OpenAI-compatible server)",
        base_url="", key_env="", signup_url="", needs_key=False, local=False,
        free_note="Works with LM Studio, llama.cpp, vLLM, Together, Cerebras, Mistral and similar; costs and limits "
                  "depend on the service you point it at.",
        privacy_note="Prompts go to whatever server you configure - check its policy (a server on your own machine "
                     "keeps them local).",
        model_hints=(), fallback_models=(), rpm=20),
}


# The built-in services' usual key variables, and whose they are: GEMINI_API_KEY is Google's key and is only ever sent to
# Google's own server, whatever a settings file says (see ``client.AIClient.from_config``).
KEY_HOMES: dict[str, Preset] = {p.key_env: p for p in PRESETS.values() if p.key_env}


def get_preset(provider: str) -> Preset:
    key = str(provider or "").strip().lower()
    try:
        return PRESETS[key]
    except KeyError:
        raise ValueError(f"unknown AI provider {provider!r}; expected one of {', '.join(PRESETS)}") from None


# ----------------------------------------------------------------------------- model ranking
def _entry_id(entry) -> str:
    if isinstance(entry, str):
        raw = entry
    elif isinstance(entry, dict):
        raw = entry.get("id") or entry.get("name") or entry.get("model") or ""
    else:
        raw = ""
    raw = str(raw).strip()
    return raw[len("models/"):] if raw.lower().startswith("models/") else raw


def _is_zero_price(value) -> bool:
    try:
        return float(str(value).strip()) == 0.0
    except (TypeError, ValueError):
        return False


def _is_free(entry, model_id: str) -> bool:
    if model_id.lower().endswith(":free"):
        return True
    pricing = entry.get("pricing") if isinstance(entry, dict) else None
    return (isinstance(pricing, dict) and _is_zero_price(pricing.get("prompt"))
            and _is_zero_price(pricing.get("completion")))


def _makes_text(entry) -> bool:
    arch = entry.get("architecture") if isinstance(entry, dict) else None
    outs = arch.get("output_modalities") if isinstance(arch, dict) else None
    return not isinstance(outs, list) or not outs or "text" in outs


def _is_chat_id(model_id: str) -> bool:
    low = model_id.lower()
    if any(s in low for s in _NON_CHAT_SUBSTRINGS):
        return False
    return not (set(_TOKEN_SPLIT.split(low)) & _NON_CHAT_TOKENS)


def _version_of(model_id: str) -> tuple[int, int, int]:
    """First version-looking token ('2.5', '3', 'llama3.2', 'qwen2.5' ...) as a comparable tuple; (-1, 0, 0) if none.

    Parameter counts ('20b', '120b', 'e4b') and dates ('2507', '001') are not versions.
    """
    name = model_id.lower().rsplit("/", 1)[-1].split(":", 1)[0]
    for token in _TOKEN_SPLIT.split(name):
        m = _VERSION_TOKEN.match(token)
        if m:
            parts = [int(p) for p in m.group(1).split(".")]
            return tuple((parts + [0, 0, 0])[:3])  # type: ignore[return-value]
    return (-1, 0, 0)


def _size_b(model_id: str) -> float | None:
    m = _SIZE_B.search(model_id.lower())
    return float(m.group(1)) if m else None


def _hint_index(preset: Preset, low: str) -> int:
    for i, hint in enumerate(preset.model_hints):
        if hint in low:
            return i
    return len(preset.model_hints)


def rank_models(preset: Preset, models: list[dict]) -> list[str]:
    """Usable chat model ids from a ``/models`` listing, best first.

    Order: hint index (unknown hints last), then - on local presets - "not too big for a laptop", then specialised
    models (coder / vision / math) after general ones, then version number descending (3.1 before 2.5), then stable
    before preview/exp/beta, then bigger before smaller, then by id so the result is deterministic.
    """
    seen: set[str] = set()
    ranked: list[tuple[tuple, str]] = []
    for entry in models or []:
        mid = _entry_id(entry)
        low = mid.lower()
        if not mid or low in seen or not _is_chat_id(mid):
            continue
        if preset.id_must_contain and not any(s in low for s in preset.id_must_contain):
            continue
        if preset.id == "openrouter" and not (_is_free(entry, mid) and _makes_text(entry)):
            continue
        seen.add(low)
        size = _size_b(mid)
        oversize = int(preset.local and size is not None and size > _LOCAL_MAX_B)
        topical = int(any(t in low for t in _TOPIC_PENALTY))
        unstable = int(bool(set(_TOKEN_SPLIT.split(low)) & _UNSTABLE_TOKENS))
        ver = _version_of(mid)
        key = (_hint_index(preset, low), oversize, topical, tuple(-v for v in ver), unstable, -(size or 0.0), low)
        ranked.append((key, mid))
    ranked.sort(key=lambda kv: kv[0])
    return [mid for _, mid in ranked]


def choose_model(preset: Preset, models: list[dict]) -> str | None:
    ranked = rank_models(preset, models)
    return ranked[0] if ranked else None
