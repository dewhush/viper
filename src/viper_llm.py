"""
VIPER — LLM Manager with dual-model fallback chain.

Models (same endpoint, no API key):
  Primary:   oc/deepseek-v4-flash-free  (non-streaming JSON)
  Secondary: ollama/minimax-m2.5        (SSE streaming format)
Both hit http://localhost:20128/v1/chat/completions

Last-resort: Deterministic defaults (no hang, no crash).

Health state persisted to viper_llm_state.json.
"""

import json
import logging
import os
import re
import urllib.request
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "viper_llm_state.json")

LOCAL_ENDPOINT = "http://localhost:20128/v1/chat/completions"

TIMEOUT_PRIMARY = 5
TIMEOUT_FALLBACK = 10

PRIMARY_MODEL = "oc/deepseek-v4-flash-free"
FALLBACK_MODEL = "ollama/minimax-m2.5"


class LLMManager:
    """LLM request handler with silent dual-model fallback chain."""

    def __init__(self, env_path: Optional[str] = None):
        self._primary_available: bool = True
        self._secondary_available: bool = True
        self._env_path = env_path or os.path.join(BASE_DIR, ".env")
        self._load_state()

    # ── Public API ──────────────────────────────────────────────

    def inline_completion(
        self,
        prompt: str,
        system_prompt: str = "You are a trading assistant. Answer YES or NO only.",
    ) -> str:
        """Short completion for strategy decisions. Returns 'no' on failure."""
        result = self._try_completion(prompt, system_prompt, max_tokens=50)
        if result:
            cleaned = result.strip().lower().rstrip(".,!?")
            if cleaned in ("yes", "no", "y", "n"):
                return "yes" if cleaned in ("yes", "y") else "no"
            first = cleaned.split()[0] if cleaned.split() else "no"
            return first if first in ("yes", "no") else "no"
        return "no"

    def full_completion(
        self,
        prompt: str,
        system_prompt: str = "You are a financial market analyst. Provide concise analysis.",
        max_tokens: int = 300,
    ) -> str:
        """Verbose completion for market analysis."""
        result = self._try_completion(prompt, system_prompt, max_tokens=max_tokens)
        if result:
            return result.strip()
        return self._deterministic_analysis(prompt)

    def health_status(self) -> dict:
        return {
            "primary_available": self._primary_available,
            "secondary_available": self._secondary_available,
            "last_check_ts": datetime.now(timezone.utc).isoformat(),
        }

    def reset_health(self):
        self._primary_available = True
        self._secondary_available = True
        self._save_state()
        logger.info("LLM health flags reset")

    # ── Internal ────────────────────────────────────────────────

    def _try_completion(
        self, prompt: str, system_prompt: str, max_tokens: int
    ) -> Optional[str]:
        """Try completion with fallback chain. Returns None if all fail."""
        # 1. Primary model (non-streaming JSON)
        if self._primary_available:
            try:
                result = self._call_llm(
                    prompt, system_prompt, max_tokens,
                    model=PRIMARY_MODEL, timeout=TIMEOUT_PRIMARY, is_sse=False,
                )
                if result:
                    self._primary_available = True
                    self._save_state()
                    return result
            except Exception as e:
                logger.warning("Primary LLM (%s): %s", PRIMARY_MODEL, e)
                self._primary_available = False
                self._save_state()

        # 2. Fallback model (SSE streaming)
        if self._secondary_available:
            try:
                result = self._call_llm(
                    prompt, system_prompt, max_tokens,
                    model=FALLBACK_MODEL, timeout=TIMEOUT_FALLBACK, is_sse=True,
                )
                if result:
                    self._secondary_available = True
                    self._save_state()
                    return result
            except Exception as e:
                logger.warning("Fallback LLM (%s): %s", FALLBACK_MODEL, e)
                self._secondary_available = False
                self._save_state()

        logger.warning("All LLM providers unavailable — using deterministic output")
        return None

    def _call_llm(
        self, prompt: str, system_prompt: str, max_tokens: int,
        model: str, timeout: int, is_sse: bool,
    ) -> Optional[str]:
        """POST to local LLM endpoint (no API key)."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens * 3 if model == PRIMARY_MODEL else max_tokens,
            "temperature": 0.1,
            "stream": is_sse,  # Request SSE explicitly if needed
        }).encode()

        req = urllib.request.Request(
            LOCAL_ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        body = resp.read().decode()

        if is_sse:
            return self._parse_sse_response(body)
        else:
            return self._parse_json_response(body)

    @staticmethod
    def _parse_json_response(body: str) -> Optional[str]:
        """Parse non-streaming JSON response (deepseek format)."""
        body = body.split("data: [DONE]")[0].strip()
        data = json.loads(body)
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        if not content.strip():
            content = msg.get("reasoning_content") or ""
        return content.strip() if content.strip() else None

    @staticmethod
    def _parse_sse_response(body: str) -> Optional[str]:
        """Parse SSE streaming response (minimax format)."""
        content_parts = []
        reasoning_parts = []
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])  # Remove "data: "
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices", [])
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            if "content" in delta and delta["content"]:
                content_parts.append(delta["content"])
            if "reasoning_content" in delta and delta["reasoning_content"]:
                reasoning_parts.append(delta["reasoning_content"])
        content = "".join(content_parts)
        if not content:
            content = "".join(reasoning_parts)
        return content.strip() if content.strip() else None

    @staticmethod
    def _deterministic_analysis(prompt: str) -> str:
        """Deterministic analysis when LLM is down."""
        sym_match = re.search(r'\b([A-Z]{2,10}/?[A-Z]{2,10})\b', prompt)
        symbol = sym_match.group(1) if sym_match else "market"
        price_match = re.search(r'\$?(\d+\.?\d*)', prompt)
        price = price_match.group(1) if price_match else "N/A"
        return (
            f"[LLM unavailable — deterministic analysis]\n"
            f"Symbol: {symbol} | Last price: {price}\n"
            f"Assessment: Awaiting LLM connectivity for detailed analysis. "
            f"Using technical indicators from StrategySelector as primary signal source."
        )

    def _load_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE) as f:
                    state = json.load(f)
                self._primary_available = state.get("primary_available", True)
                self._secondary_available = state.get("secondary_available", True)
        except Exception:
            pass

    def _save_state(self):
        try:
            state = {
                "primary_available": self._primary_available,
                "secondary_available": self._secondary_available,
                "updated_ts": datetime.now(timezone.utc).isoformat(),
            }
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass


_manager: Optional[LLMManager] = None


def get_llm() -> LLMManager:
    global _manager
    if _manager is None:
        _manager = LLMManager()
    return _manager
