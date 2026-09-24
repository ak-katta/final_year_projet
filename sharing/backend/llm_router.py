"""
llm_router.py — Two-provider LLM router: Groq + Gemini.

Responsibilities:
  • Try providers in priority order (Groq first — faster + cheaper)
  • On 429 / 404 / 5xx / timeout, cool down that provider and switch
  • Log every call to state.llm_calls
  • Track tokens + estimated cost
  • Per-purpose model selection (cheap for classify, smart for plan)

Design:
  • Each provider is an adapter implementing `call(prompt, system, model)`
  • All errors normalized to RateLimitError / NotFoundError / AuthError / TransientError
  • Cooldowns persist to state so they survive save/resume
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

from state import QAState, LLMCall

load_dotenv()


# ═══════════════════════════════════════════════════════════════
# CANONICAL ERRORS
# ═══════════════════════════════════════════════════════════════

class LLMError(Exception):
    retryable = False

class RateLimitError(LLMError):
    """429, quota exhausted. Retryable after cooldown."""
    retryable = True
    def __init__(self, msg: str, retry_after: Optional[float] = None):
        super().__init__(msg)
        self.retry_after = retry_after

class NotFoundError(LLMError):
    """404, model not found. Not retryable without fix."""
    retryable = False

class AuthError(LLMError):
    """401/403, invalid key. Not retryable."""
    retryable = False

class TransientError(LLMError):
    """5xx, timeout, network blip. Retryable."""
    retryable = True

class AllProvidersFailed(LLMError):
    """Both providers are cooling down or failed."""
    retryable = False


# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProviderConfig:
    name: str
    provider_type: str = ""
    api_key: str = ""
    enabled: bool = True
    priority: int = 100
    default_model: str = ""
    model_by_purpose: dict[str, str] = field(default_factory=dict)
    cost_per_1m_prompt: float = 0.0
    cost_per_1m_completion: float = 0.0


# Model tiers per provider — router picks based on purpose.
# Kept to one real, confirmed-working model per provider for now rather than
# guessing at names for a "cheap" tier that may not exist on the account.
MODEL_TIERS = {
    "groq": {
        "cheap":  "openai/gpt-oss-120b",
        "medium": "openai/gpt-oss-120b",
        "smart":  "openai/gpt-oss-120b",
    },
    "gemini": {
        "cheap":  "gemini-3.6-flash",
        "medium": "gemini-3.6-flash",
        "smart":  "gemini-3.6-flash",
    },
}

# Which tier each purpose needs
PURPOSE_TIER = {
    "classify":     "cheap",
    "step_convert": "cheap",
    "repair":       "cheap",
    "plan":         "smart",
    "judge":        "smart",
    "report":       "medium",
    "generic":      "medium",
}


# ═══════════════════════════════════════════════════════════════
# PROVIDER ADAPTERS
# ═══════════════════════════════════════════════════════════════

class ProviderAdapter(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config
        self.name = config.name

    @abstractmethod
    def call(self, prompt: str, system: str, model: str) -> dict:
        """Returns: {text, prompt_tokens, completion_tokens}"""
        ...

    def _classify_exception(self, e: Exception) -> LLMError:
        """Map SDK-specific errors → canonical errors."""
        msg = str(e).lower()
        status = getattr(e, "status_code", None) or getattr(e, "code", None)

        if status == 429 or "rate limit" in msg or "quota" in msg or "too many requests" in msg:
            retry_after = None
            resp = getattr(e, "response", None)
            if resp is not None:
                ra = getattr(resp, "headers", {}).get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (ValueError, TypeError):
                        pass
            return RateLimitError(str(e), retry_after=retry_after)

        if status == 404 or "not found" in msg or "does not exist" in msg:
            return NotFoundError(str(e))
        if status in (401, 403) or "invalid api key" in msg or "unauthorized" in msg:
            return AuthError(str(e))
        if status is not None and 500 <= status < 600:
            return TransientError(str(e))
        if "timeout" in msg or "connection" in msg or "temporarily" in msg:
            return TransientError(str(e))
        return TransientError(str(e))


class GroqAdapter(ProviderAdapter):

    def call(self, prompt: str, system: str, model: str) -> dict:
        from groq import Groq

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            client = Groq(api_key=self.config.api_key)
            r = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
            )
            return {
                "text": r.choices[0].message.content or "",
                "prompt_tokens": r.usage.prompt_tokens if r.usage else 0,
                "completion_tokens": r.usage.completion_tokens if r.usage else 0,
            }
        except LLMError:
            raise
        except Exception as e:
            raise self._classify_exception(e)


class GeminiAdapter(ProviderAdapter):
    """Uses the google-genai SDK (not the older google-generativeai one)."""

    def call(self, prompt: str, system: str, model: str) -> dict:
        from google import genai
        from google.genai import types

        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        try:
            client = genai.Client(api_key=self.config.api_key)
            r = client.models.generate_content(
                model=model,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            usage = getattr(r, "usage_metadata", None)
            return {
                "text": getattr(r, "text", "") or "",
                "prompt_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
                "completion_tokens": (
                    getattr(usage, "candidates_token_count", 0) if usage else 0
                ),
            }
        except LLMError:
            raise
        except Exception as e:
            raise self._classify_exception(e)


# ═══════════════════════════════════════════════════════════════
# THE ROUTER
# ═══════════════════════════════════════════════════════════════

class LLMRouter:
    """
    Two-provider router: Groq → Gemini (fallback).

    Usage:
        router = LLMRouter.from_env()
        text = router.ask("classify: ...", state, purpose="classify")
    """

    # Cooldowns (seconds)
    COOLDOWN_RATE_LIMIT = 60.0
    COOLDOWN_NOT_FOUND  = 3600.0
    COOLDOWN_AUTH       = 3600.0
    COOLDOWN_TRANSIENT  = 15.0

    def __init__(self, adapters: list[ProviderAdapter]):
        self.adapters = sorted(
            [a for a in adapters if a.config.enabled],
            key=lambda a: a.config.priority,
        )
        self._stats: dict[str, dict] = {
            a.name: {"calls": 0, "failures": 0, "total_latency_ms": 0.0}
            for a in self.adapters
        }

    # ── Factory ───────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "LLMRouter":
        """
        Picks up keys named either GROQ_API_KEY_1 / GROQ_API_KEY_2 (what's in
        the .env right now) or a bare GROQ_API_KEY. Same for Gemini. Each key
        found becomes its own adapter so the router can fail over from one
        key to the next, not just one provider to the next.
        """
        adapters: list[ProviderAdapter] = []
        priority = 0

        for name, key in cls._keys_from_env("GROQ_API_KEY", label="groq"):
            priority += 1
            adapters.append(GroqAdapter(ProviderConfig(
                name=name, provider_type="groq", api_key=key, priority=priority,
                default_model="openai/gpt-oss-120b",
                cost_per_1m_prompt=0.05, cost_per_1m_completion=0.08,
            )))

        for name, key in cls._keys_from_env("GEMINI_API_KEY", "GOOGLE_API_KEY", label="gemini"):
            priority += 1
            adapters.append(GeminiAdapter(ProviderConfig(
                name=name, provider_type="gemini", api_key=key, priority=priority,
                default_model="gemini-3.6-flash",
                cost_per_1m_prompt=0.10, cost_per_1m_completion=0.40,
            )))

        if not adapters:
            raise RuntimeError(
                "No LLM providers configured. Set GROQ_API_KEY_1 and/or "
                "GEMINI_API_KEY_1 (or the unsuffixed versions) in .env."
            )
        return cls(adapters)

    @staticmethod
    def _keys_from_env(*env_names: str, label: str = None) -> list[tuple[str, str]]:
        """
        env_names: one or more env var basenames to check (e.g. GEMINI_API_KEY
        and GOOGLE_API_KEY as aliases). Looks for _1, _2, _3 suffixes first;
        if none of those exist, falls back to the bare name as a single key.
        """
        found = []
        for i in (1, 2, 3):
            for env_name in env_names:
                key = os.environ.get(f"{env_name}_{i}")
                if key:
                    found.append((f"{label}_{i}", key))
                    break
        if not found:
            for env_name in env_names:
                key = os.environ.get(env_name)
                if key:
                    found.append((f"{label}_1", key))
                    break
        return found

    # ── Public API ────────────────────────────────────────────

    def ask(
        self,
        prompt: str,
        state: QAState,
        system: str = "",
        purpose: str = "generic",
        max_attempts: Optional[int] = None,
    ) -> str:
        """
        Send a prompt to the first available provider.
        On failure, cool that provider down and try the next.
        Logs everything to state.llm_calls.

        The attempt budget defaults to the number of configured providers
        (minimum 3). A fixed budget of 3 meant that with 4 keys in .env the
        router would give up before it had even tried the last one.
        """
        budget = max_attempts if max_attempts is not None else max(3, len(self.adapters))
        attempts = 0
        last_error: Optional[Exception] = None
        tried: set[str] = set()
        failures: dict[str, str] = {}

        while attempts < budget:
            adapter = self._pick_next(state, tried)
            if adapter is None:
                if not tried:
                    raise AllProvidersFailed(
                        f"All providers cooling down: "
                        f"{self.unavailable_providers(state)}"
                    )
                # Cycle complete — reset and try again
                tried.clear()
                attempts += 1
                continue

            tried.add(adapter.name)
            model = self._model_for(adapter, purpose)
            t0 = time.time()

            try:
                result = adapter.call(prompt, system, model)
                latency_ms = (time.time() - t0) * 1000
                self._record_success(state, adapter, model, purpose,
                                     result, latency_ms)
                return result["text"]

            except RateLimitError as e:
                latency_ms = (time.time() - t0) * 1000
                cooldown = e.retry_after or self.COOLDOWN_RATE_LIMIT
                state.set_provider_cooldown(adapter.name, cooldown)
                self._record_failure(state, adapter, model, purpose,
                                     latency_ms, f"rate_limit: {e}")
                failures[adapter.name] = "rate_limit"
                last_error = e
                attempts += 1

            except NotFoundError as e:
                latency_ms = (time.time() - t0) * 1000
                state.set_provider_cooldown(adapter.name, self.COOLDOWN_NOT_FOUND)
                self._record_failure(state, adapter, model, purpose,
                                     latency_ms, f"not_found: {e}")
                failures[adapter.name] = "not_found"
                last_error = e
                attempts += 1

            except AuthError as e:
                latency_ms = (time.time() - t0) * 1000
                state.set_provider_cooldown(adapter.name, self.COOLDOWN_AUTH)
                self._record_failure(state, adapter, model, purpose,
                                     latency_ms, f"auth: {e}")
                failures[adapter.name] = "auth"
                last_error = e
                attempts += 1

            except TransientError as e:
                latency_ms = (time.time() - t0) * 1000
                cooldown = self.COOLDOWN_TRANSIENT * (2 ** attempts)
                state.set_provider_cooldown(adapter.name, cooldown)
                self._record_failure(state, adapter, model, purpose,
                                     latency_ms, f"transient: {e}")
                failures[adapter.name] = "transient"
                last_error = e
                attempts += 1

            except Exception as e:
                latency_ms = (time.time() - t0) * 1000
                self._record_failure(state, adapter, model, purpose,
                                     latency_ms, f"unexpected: {e}")
                failures[adapter.name] = "unexpected"
                last_error = e
                attempts += 1

        # Surface *which* provider failed and *how* — an all-auth failure
        # means the keys in .env are bad, which is worth saying plainly
        # rather than burying under a provider SDK traceback.
        detail = ", ".join(f"{n}={why}" for n, why in sorted(failures.items())) or "none tried"
        if failures and set(failures.values()) == {"auth"}:
            raise AllProvidersFailed(
                f"Every configured LLM provider rejected the API key ({detail}). "
                f"Check GROQ_API_KEY_*/GEMINI_API_KEY_* in backend/.env. "
                f"Last error: {last_error}"
            )
        raise AllProvidersFailed(
            f"Exhausted {attempts} attempts across {len(failures)} provider(s) "
            f"[{detail}]. Last error: {last_error}"
        )

    # ── Provider selection ────────────────────────────────────

    def _pick_next(
        self, state: QAState, tried: set[str]
    ) -> Optional[ProviderAdapter]:
        for adapter in self.adapters:
            if adapter.name in tried:
                continue
            if not state.provider_available(adapter.name):
                continue
            return adapter
        return None

    def _model_for(self, adapter: ProviderAdapter, purpose: str) -> str:
        cfg = adapter.config
        if purpose in cfg.model_by_purpose:
            return cfg.model_by_purpose[purpose]
        tier = PURPOSE_TIER.get(purpose, "medium")
        tier_map = MODEL_TIERS.get(adapter.config.provider_type, {})
        if tier in tier_map:
            return tier_map[tier]
        return cfg.default_model or "default"

    # ── Logging ───────────────────────────────────────────────

    def _record_success(
        self, state: QAState, adapter: ProviderAdapter, model: str,
        purpose: str, result: dict, latency_ms: float,
    ) -> None:
        pt = int(result.get("prompt_tokens", 0))
        ct = int(result.get("completion_tokens", 0))
        cost = self._estimate_cost(adapter, pt, ct)

        state.add_llm_call(LLMCall(
            provider=adapter.name,
            model=model,
            purpose=purpose,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=latency_ms,
            ok=True,
        ))
        state.estimated_cost_usd += cost

        s = self._stats[adapter.name]
        s["calls"] += 1
        s["total_latency_ms"] += latency_ms

        # Clear stale cooldown on success
        state.provider_cooldowns.pop(adapter.name, None)

    def _record_failure(
        self, state: QAState, adapter: ProviderAdapter, model: str,
        purpose: str, latency_ms: float, error: str,
    ) -> None:
        state.add_llm_call(LLMCall(
            provider=adapter.name,
            model=model,
            purpose=purpose,
            latency_ms=latency_ms,
            ok=False,
            error=error[:300],
        ))
        self._stats[adapter.name]["failures"] += 1

    def _estimate_cost(
        self, adapter: ProviderAdapter, prompt_tokens: int, completion_tokens: int
    ) -> float:
        cfg = adapter.config
        return (
            prompt_tokens / 1_000_000 * cfg.cost_per_1m_prompt
            + completion_tokens / 1_000_000 * cfg.cost_per_1m_completion
        )

    # ── Introspection ─────────────────────────────────────────

    def stats(self) -> dict[str, dict]:
        return {
            name: {
                **s,
                "avg_latency_ms": round(
                    s["total_latency_ms"] / s["calls"], 1
                ) if s["calls"] else 0.0,
                "success_rate": round(
                    (s["calls"] - s["failures"]) / s["calls"], 3
                ) if s["calls"] else 0.0,
            }
            for name, s in self._stats.items()
        }

    def available_providers(self, state: QAState) -> list[str]:
        return [
            a.name for a in self.adapters
            if state.provider_available(a.name)
        ]

    def unavailable_providers(self, state: QAState) -> dict[str, float]:
        now = time.time()
        return {
            name: round(until - now, 1)
            for name, until in state.provider_cooldowns.items()
            if until > now
        }


# ═══════════════════════════════════════════════════════════════
# USAGE
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    from state import QAState

    router = LLMRouter.from_env()
    state = QAState(url="https://example.com")

    print("Providers:")
    for a in router.adapters:
        print(f"  - {a.name} (priority={a.config.priority})")

    print("\nAvailable:", router.available_providers(state))

    try:
        text = router.ask("Reply with exactly: OK", state, purpose="classify")
        print(f"\nGroq response: {text!r}")
    except AllProvidersFailed as e:
        print(f"\nFailed: {e}")

    # Force a fallback test: cool down every groq key manually
    for a in router.adapters:
        if a.config.provider_type == "groq":
            state.set_provider_cooldown(a.name, 60)
    print("\nAfter cooling down Groq:", router.available_providers(state))

    try:
        text = router.ask("Reply with exactly: OK", state, purpose="classify")
        print(f"Fallback response: {text!r}")
    except AllProvidersFailed as e:
        print(f"Failed: {e}")

    print("\nStats:", router.stats())
    print("Cost: $", round(state.estimated_cost_usd, 6))