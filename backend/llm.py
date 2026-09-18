"""
Dynamic multi-provider LLM with automatic failover + streaming.
Usage:
    from llm import llm

    # Non-streaming
    print(llm.chat("Hello"))

    # Streaming
    for chunk in llm.stream("Tell me a story"):
        print(chunk, end="", flush=True)
"""

import os
import time
import logging
from typing import Optional, List, Dict, Any, Iterator
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("llm")


@dataclass
class ProviderConfig:
    name: str
    api_key: str
    model: str
    client: Any
    provider_type: str  # "gemini", "groq"
    cooldown_until: float = 0.0
    failures: int = 0


class LLM:
    """Dynamic LLM with auto-failover across multiple providers/keys."""

    COOLDOWN_SECONDS = 60
    MAX_COOLDOWN = 600

    def __init__(self):
        self.providers: List[ProviderConfig] = []
        self._load_providers()
        if not self.providers:
            raise RuntimeError("No LLM providers configured. Check your .env file.")

    # ---------- Provider loading ----------
    def _load_providers(self):
        # Gemini
        from google import genai as google_genai
        for i in (1, 2):
            key = os.getenv(f"GEMINI_API_KEY_{i}")
            if not key:
                continue
            try:
                self.providers.append(ProviderConfig(
                    name=f"gemini_{i}",
                    api_key=key,
                    model="gemini-3.6-flash",
                    client=google_genai.Client(api_key=key),
                    provider_type="gemini",
                ))
                logger.info(f"Loaded gemini_{i}")
            except Exception as e:
                logger.warning(f"Failed to load gemini_{i}: {e}")

        # Groq
        from groq import Groq
        for i in (1, 2):
            key = os.getenv(f"GROQ_API_KEY_{i}")
            if not key:
                continue
            try:
                self.providers.append(ProviderConfig(
                    name=f"groq_{i}",
                    api_key=key,
                    model="openai/gpt-oss-120b",
                    client=Groq(api_key=key),
                    provider_type="groq",
                ))
                logger.info(f"Loaded groq_{i}")
            except Exception as e:
                logger.warning(f"Failed to load groq_{i}: {e}")

    # ---------- Health tracking ----------
    def _is_available(self, p: ProviderConfig) -> bool:
        return time.time() >= p.cooldown_until

    def _mark_failure(self, p: ProviderConfig, err: Exception):
        p.failures += 1
        cooldown = min(self.COOLDOWN_SECONDS * (2 ** (p.failures - 1)), self.MAX_COOLDOWN)
        p.cooldown_until = time.time() + cooldown
        logger.warning(f"[{p.name}] failed ({p.failures}x). Cooldown {cooldown}s. Error: {err}")

    def _mark_success(self, p: ProviderConfig):
        p.failures = 0
        p.cooldown_until = 0.0

    def _candidates(self) -> List[ProviderConfig]:
        return sorted(
            self.providers,
            key=lambda p: (not self._is_available(p), p.failures),
        )

    # ---------- Non-streaming per provider ----------
    def _call_gemini(self, p: ProviderConfig, prompt: str, system: Optional[str]) -> str:
        from google.genai import types
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        resp = p.client.models.generate_content(
            model=p.model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        return resp.text

    def _call_openai_compatible(self, p, prompt, system, temperature, max_tokens) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = p.client.chat.completions.create(
            model=p.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        return resp.choices[0].message.content

    # ---------- Streaming per provider ----------
    def _stream_gemini(self, p: ProviderConfig, prompt: str,
                       system: Optional[str],
                       temperature: float, max_tokens: int) -> Iterator[str]:
        from google.genai import types
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        stream = p.client.models.generate_content_stream(
            model=p.model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        for chunk in stream:
            # chunk.text can be None on some control chunks
            if getattr(chunk, "text", None):
                yield chunk.text

    def _stream_openai_compatible(self, p: ProviderConfig, prompt: str,
                                   system: Optional[str],
                                   temperature: float, max_tokens: int) -> Iterator[str]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        stream = p.client.chat.completions.create(
            model=p.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    # ---------- Public: non-streaming ----------
    def chat(self, prompt: str, system: Optional[str] = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> str:
        last_error = None
        for p in self._candidates():
            if not self._is_available(p):
                continue
            try:
                logger.info(f"Trying provider: {p.name}")
                if p.provider_type == "gemini":
                    out = self._call_gemini(p, prompt, system)
                else:
                    out = self._call_openai_compatible(p, prompt, system, temperature, max_tokens)
                self._mark_success(p)
                logger.info(f"Success with {p.name}")
                return out
            except Exception as e:
                last_error = e
                self._mark_failure(p, e)
                continue
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    # ---------- Public: streaming ----------
    def stream(self, prompt: str, system: Optional[str] = None,
               temperature: float = 0.7, max_tokens: int = 2048) -> Iterator[str]:
        """
        Stream tokens with automatic failover.

        Failover only happens if the error occurs BEFORE the first token
        is emitted. Once we've sent a token to the caller, we commit to
        that provider — mid-stream errors raise.
        """
        last_error = None
        for p in self._candidates():
            if not self._is_available(p):
                continue

            gen = None
            try:
                logger.info(f"Trying stream provider: {p.name}")
                if p.provider_type == "gemini":
                    gen = self._stream_gemini(p, prompt, system, temperature, max_tokens)
                else:
                    gen = self._stream_openai_compatible(p, prompt, system, temperature, max_tokens)

                # Pull the first chunk here so failures during connection /
                # initial request bubble up and trigger failover.
                first_chunk = next(gen)

            except StopIteration:
                # Provider returned an empty stream — treat as failure, try next
                last_error = RuntimeError(f"{p.name} returned empty stream")
                self._mark_failure(p, last_error)
                continue
            except Exception as e:
                last_error = e
                self._mark_failure(p, e)
                continue

            # --- Committed to this provider ---
            self._mark_success(p)
            try:
                yield first_chunk
                for chunk in gen:
                    yield chunk
            except Exception as e:
                # Mid-stream failure: can't fail over cleanly.
                self._mark_failure(p, e)
                logger.error(f"[{p.name}] mid-stream error: {e}")
                raise
            return

        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    # Convenience: stream-into-string
    def stream_to_string(self, prompt: str, **kwargs) -> str:
        return "".join(self.stream(prompt, **kwargs))

    # ---------- Utilities ----------
    def __call__(self, prompt: str, **kwargs) -> str:
        return self.chat(prompt, **kwargs)

    def status(self) -> List[Dict[str, Any]]:
        now = time.time()
        return [{
            "name": p.name,
            "type": p.provider_type,
            "model": p.model,
            "failures": p.failures,
            "available": now >= p.cooldown_until,
            "cooldown_remaining": max(0, int(p.cooldown_until - now)),
        } for p in self.providers]


# Singleton
llm = LLM()


if __name__ == "__main__":
    print("--- non-streaming ---")
    print(llm.chat("Say hi in one sentence."))

    print("\n--- streaming ---")
    for chunk in llm.stream("Write a haiku about failover."):
        print(chunk, end="", flush=True)
    print()