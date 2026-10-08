"""Shared LLM client for Aipply via OpenRouter.

OpenRouter exposes an OpenAI-compatible API, so the standard ``openai`` package
works with a custom ``base_url``. All AI features (resume tailoring, cover
letters, CV scoring, CV optimization) share one client configured via
``config/settings.yaml`` (``llm`` section) and environment variables:

- ``OPENROUTER_API_KEY`` — OpenRouter API key (required for AI features)
- ``OPENAI_API_KEY``     — legacy fallback, kept for backwards compatibility

If no key is configured, every caller degrades gracefully: tailoring clones the
base resume unchanged, cover letters use the template, scoring/optimization
return heuristic results.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

from openai import OpenAI

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# openrouter/free auto-routes to the best currently-available free model,
# so it keeps working as individual free models come and go.
DEFAULT_MODEL = "openrouter/free"
DEFAULT_FALLBACK_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "qwen/qwen3.8-27b:free",
]


def _clean_text(text: str) -> str:
    """Normalize model output: strip think-blocks, markdown fences, whitespace.

    Some free routing models emit visible chain-of-thought before the final
    answer; the <think> blocks (if tagged) are removed so the JSON that
    follows can be located.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip()


def _iter_json_objects(text: str):
    """Yield candidate JSON object substrings (balanced-brace scan)."""
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start : i + 1]
                    start = None


def extract_json(text: str) -> dict | None:
    """Extract the last valid JSON object from model output.

    Reasoning models often produce braces mid-thought before the final
    answer, so later objects take priority over earlier ones.
    """
    cleaned = _clean_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    last_valid = None
    for candidate in _iter_json_objects(cleaned):
        try:
            last_valid = json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return last_valid


class LLMClient:
    """OpenRouter-backed chat client shared by all AI features."""

    def __init__(self, config: dict[str, Any] | None = None):
        """Create the client from the ``llm`` (or legacy ``openai``) config section.

        Args:
            config: Full settings dict (e.g. parsed config/settings.yaml).
        """
        self.config = config or {}
        llm_cfg = self.config.get("llm") or self.config.get("openai") or {}
        self.model = llm_cfg.get("model", DEFAULT_MODEL)
        self.temperature = llm_cfg.get("temperature", 0.7)
        # Generous default: routed free models may reason before answering
        self.max_tokens = llm_cfg.get("max_tokens", 4000)
        self.empty_retries = max(0, int(llm_cfg.get("empty_retries", 1)))
        fallbacks = llm_cfg.get("fallback_models", DEFAULT_FALLBACK_MODELS)
        self.fallback_models = [m for m in fallbacks if m and m != self.model]
        self._client: OpenAI | None = None
        self.last_error = ""

    def _api_key(self) -> str:
        key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not key:
            key = os.getenv("OPENAI_API_KEY", "").strip()
        return key

    def get_client(self) -> OpenAI | None:
        """Return a ready client, or None when no API key is configured."""
        key = self._api_key()
        if not key or key.startswith("PASTE-"):
            return None
        if self._client is None:
            self._client = OpenAI(
                api_key=key,
                base_url=OPENROUTER_BASE_URL,
                default_headers={
                    "HTTP-Referer": "https://github.com/attdobi/aipply",
                    "X-Title": "Aipply Job Application Bot",
                },
            )
        return self._client

    def chat(self, system: str, user: str, temperature: float | None = None) -> str:
        """Send a chat completion, falling back to secondary models on failure.

        Args:
            system: System prompt.
            user: User prompt.
            temperature: Optional per-call temperature override.

        Returns:
            Model output text, or "" if no key is configured or all attempts fail.
        """
        client = self.get_client()
        if client is None:
            self.last_error = "No OpenRouter API key is configured."
            return ""

        models = [self.model] + self.fallback_models
        last_exc: Exception | None = None
        self.last_error = ""
        for model in models:
            for attempt in range(self.empty_retries + 1):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        temperature=self.temperature if temperature is None else temperature,
                        max_tokens=self.max_tokens,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    )
                    choices = response.choices or []
                    if not choices:
                        self.last_error = f"{model} returned no choices"
                        logger.warning("%s", self.last_error)
                        continue
                    content = (choices[0].message.content or "").strip()
                    if content:
                        return content
                    finish_reason = getattr(choices[0], "finish_reason", None)
                    self.last_error = f"{model} returned empty content (finish_reason={finish_reason or 'unknown'})"
                    logger.warning("%s; retry %d/%d", self.last_error, attempt + 1, self.empty_retries + 1)
                except Exception as exc:
                    last_exc = exc
                    self.last_error = f"{model}: {exc}"
                    logger.warning("LLM call failed for %s: %s", model, exc)
                    break
        if last_exc:
            logger.warning("All LLM models failed; last error: %s", last_exc)
        if not self.last_error:
            self.last_error = "All configured models returned no usable content."
        return ""

    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict | None:
        """Send a chat completion and parse the JSON object from the output.

        Returns:
            Parsed dict, or None if unavailable/unparseable.
        """
        raw = self.chat(system, user, temperature=temperature)
        if not raw:
            return None
        parsed = extract_json(raw)
        if parsed is None:
            logger.warning("LLM output was not valid JSON: %.200s", raw)
        return parsed

    def available(self) -> bool:
        """True when an API key is configured and a client can be created."""
        return self.get_client() is not None
