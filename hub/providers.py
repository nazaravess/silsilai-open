"""Адаптеры к провайдерам. Два вида: OpenAI-совместимый (Groq, Google, OpenRouter, …) и Anthropic.
Любой новый провайдер с OpenAI-совместимым API добавляется одной записью в registry.yaml, без кода."""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx


@dataclass
class Reply:
    text: str
    tokens_in: int
    tokens_out: int


class ProviderError(Exception):
    pass


class Provider:
    def __init__(self, name: str, cfg: dict, timeout: float = 30.0):
        self.name = name
        self.kind = cfg["kind"]
        self.base_url = cfg["base_url"].rstrip("/")
        self.api_key = os.environ.get(cfg.get("api_key_env", ""), "")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def chat(self, model: str, system: str, messages: list[dict], max_tokens: int, json_mode: bool = False) -> Reply:
        if self.kind == "openai":
            return await self._openai(model, system, messages, max_tokens, json_mode)
        if self.kind == "anthropic":
            return await self._anthropic(model, system, messages, max_tokens)
        raise ProviderError(f"unknown provider kind {self.kind}")

    async def _openai(self, model, system, messages, max_tokens, json_mode) -> Reply:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": max_tokens,
            "temperature": 0.4,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout) as cl:
            r = await cl.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        if r.status_code == 429:
            raise ProviderError("rate_limit")
        if r.status_code >= 400:
            raise ProviderError(f"http {r.status_code}: {r.text[:200]}")
        d = r.json()
        u = d.get("usage", {})
        return Reply(
            d["choices"][0]["message"]["content"],
            int(u.get("prompt_tokens", 0)),
            int(u.get("completion_tokens", 0)),
        )

    async def _anthropic(self, model, system, messages, max_tokens) -> Reply:
        async with httpx.AsyncClient(timeout=self.timeout) as cl:
            r = await cl.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": model, "system": system, "messages": messages, "max_tokens": max_tokens},
            )
        if r.status_code == 429:
            raise ProviderError("rate_limit")
        if r.status_code >= 400:
            raise ProviderError(f"http {r.status_code}: {r.text[:200]}")
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []))
        u = d.get("usage", {})
        return Reply(text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))

    async def list_models(self) -> list[str]:
        """Какие модели реально доступны этому ключу (OpenAI-совместимый /models)."""
        if self.kind != "openai":
            return []
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
        if r.status_code >= 400:
            raise ProviderError(f"models http {r.status_code}: {r.text[:150]}")
        return sorted(x.get("id", "") for x in r.json().get("data", []))

    async def transcribe(self, model: str, audio_path: str, lang: str | None = None) -> str:
        """Голос → текст через OpenAI-совместимый endpoint (Groq Whisper)."""
        data = {"model": model}
        if lang:
            data["language"] = lang
        with open(audio_path, "rb") as f:
            async with httpx.AsyncClient(timeout=60) as cl:
                r = await cl.post(
                    f"{self.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files={"file": (os.path.basename(audio_path), f)},
                )
        if r.status_code >= 400:
            raise ProviderError(f"stt http {r.status_code}")
        return r.json().get("text", "")
