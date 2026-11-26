"""LLM router: Groq and NVIDIA (OpenAI-compatible) with an automatic fallback.

provider = "groq"   -> Groq only (all saved keys are rotated on rate limit)
provider = "nvidia" -> NVIDIA only
provider = "auto"   -> Groq first; when every Groq key errors, switch to NVIDIA.
                       After a Groq failure it is skipped for GROQ_COOLDOWN seconds
                       so the rest of the run doesn't waste time on a dead provider.
"""
import time
from typing import Callable

from groq import Groq
from openai import OpenAI

import config

PROVIDERS = ("auto", "groq", "nvidia")
GROQ_COOLDOWN = 90          # seconds to skip Groq after all its keys failed (auto mode)

_groq_dead_until: dict[str, float] = {}     # per-user cooldown, keyed by uid


class LLMError(Exception):
    pass


def normalize_provider(p: str | None) -> str:
    p = (p or "auto").strip().lower()
    return p if p in PROVIDERS else "auto"


def nvidia_client(api_key: str) -> OpenAI:
    return OpenAI(base_url=config.NVIDIA_BASE_URL, api_key=api_key, timeout=90)


class LLM:
    """One per run. `complete()` returns the text of the reply."""

    def __init__(self, uid: str, settings: dict, secrets: dict, log: Callable | None = None):
        self.uid = uid
        self.provider = normalize_provider(settings.get("llm_provider"))
        self.groq_keys = [k for k in secrets.get("groq_keys", []) if k]
        self.nvidia_key = secrets.get("nvidia_key") or config.NVIDIA_API_KEY
        self.models = {
            ("groq", "chat"): settings.get("chat_model") or config.DEFAULT_CHAT_MODEL,
            ("groq", "vision"): settings.get("vision_model") or config.DEFAULT_VISION_MODEL,
            ("nvidia", "chat"): settings.get("nvidia_chat_model") or config.DEFAULT_NVIDIA_CHAT_MODEL,
            ("nvidia", "vision"): settings.get("nvidia_vision_model") or config.DEFAULT_NVIDIA_VISION_MODEL,
        }
        self.log = log or (lambda *a, **k: None)
        self.last_used = ""

    # ------------------------------------------------------------ setup ----
    def order(self) -> list[str]:
        have = {"groq": bool(self.groq_keys), "nvidia": bool(self.nvidia_key)}
        if self.provider == "auto":
            names = ["groq", "nvidia"]
            if time.time() < _groq_dead_until.get(self.uid, 0) and have["nvidia"]:
                names = ["nvidia"]          # Groq was just exhausted - go straight to NVIDIA
        else:
            names = [self.provider]
        return [n for n in names if have[n]]

    def ready(self) -> bool:
        return bool(self.groq_keys or self.nvidia_key) if self.provider == "auto" else bool(
            self.groq_keys if self.provider == "groq" else self.nvidia_key)

    def missing_message(self) -> str:
        if self.provider == "groq":
            return "Add a Groq API key in Settings first (or switch the AI provider to Auto / NVIDIA)."
        if self.provider == "nvidia":
            return "Add an NVIDIA API key in Settings first (or switch the AI provider to Auto / Groq)."
        return "Add a Groq or NVIDIA API key in Settings first."

    # --------------------------------------------------------- the call ----
    def complete(self, kind: str, messages, *, temperature=0.3, max_tokens=1024, json_mode=False) -> str:
        """kind = 'chat' | 'vision'. `messages` may be a list, or a function(provider) -> list
        (needed for images, which NVIDIA wants smaller)."""
        order = self.order()
        if not order:
            raise LLMError(self.missing_message())
        errors = []
        for name in order:
            msgs = messages(name) if callable(messages) else messages
            try:
                text = (self._groq if name == "groq" else self._nvidia)(
                    kind, msgs, temperature, max_tokens, json_mode)
                if self.last_used and self.last_used != name:
                    self.log(f"  ↪ using {name.upper()}", "muted")
                self.last_used = name
                return text
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {str(exc)[:160]}")
                if name == "groq" and self.provider == "auto":
                    _groq_dead_until[self.uid] = time.time() + GROQ_COOLDOWN
                if name != order[-1]:
                    self.log(f"  ⚠ {name.upper()} failed ({str(exc)[:100]}) → switching to {order[order.index(name) + 1].upper()}", "warn")
        raise LLMError(" | ".join(errors))

    def _groq(self, kind, messages, temperature, max_tokens, json_mode) -> str:
        last = None
        for _ in range(len(self.groq_keys)):
            key = self.groq_keys[0]
            try:
                kw = {"response_format": {"type": "json_object"}} if json_mode else {}
                r = Groq(api_key=key, timeout=60).chat.completions.create(
                    model=self.models[("groq", kind)], messages=messages,
                    temperature=temperature, max_completion_tokens=max_tokens, **kw)
                return r.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001
                last = exc
                self.groq_keys.append(self.groq_keys.pop(0))      # rotate to the next key
        raise last or LLMError("no Groq key")

    def _nvidia(self, kind, messages, temperature, max_tokens, json_mode) -> str:
        # NVIDIA NIM doesn't reliably support response_format on every model; the prompts
        # already demand JSON and ai.parse_json() is tolerant, so it is not sent.
        r = nvidia_client(self.nvidia_key).chat.completions.create(
            model=self.models[("nvidia", kind)], messages=messages,
            temperature=temperature, max_tokens=max_tokens)
        return r.choices[0].message.content or ""
