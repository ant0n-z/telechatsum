"""Единый интерфейс к LLM-провайдерам. Выбор провайдера — в config.toml, секция [llm]."""
from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Ошибка обращения к LLM-провайдеру."""


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    async def _complete(self, system: str, user: str) -> str: ...

    async def complete(self, system: str, user: str, retries: int = 3) -> str:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                text = (await self._complete(system, user)).strip()
                if not text:
                    raise LLMError("пустой ответ модели")
                return text
            except Exception as e:  # noqa: BLE001 - у разных SDK разные классы ошибок
                last = e
                log.warning("%s: попытка %d/%d не удалась: %s", self.name, attempt + 1, retries, e)
                if attempt < retries - 1:
                    await asyncio.sleep(2 * 2**attempt)
        raise LLMError(f"{self.name}: {last}") from last


def _api_key(cfg: dict, default_env: str) -> str:
    env_name = cfg.get("api_key_env", default_env)
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise SystemExit(f"Не задана переменная окружения {env_name} с ключом LLM-провайдера")
    return key


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, cfg: dict):
        from anthropic import AsyncAnthropic  # импорт здесь, чтобы нужен был только выбранный SDK

        self.client = AsyncAnthropic(api_key=_api_key(cfg, "ANTHROPIC_API_KEY"))
        self.model = cfg["model"]
        self.max_tokens = int(cfg.get("max_tokens", 2000))

    async def _complete(self, system: str, user: str) -> str:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")


class OpenAIProvider(LLMProvider):
    """OpenAI и любые OpenAI-совместимые API (OpenRouter, DeepSeek, Groq, Ollama, vLLM...)."""

    def __init__(self, cfg: dict, *, name: str, default_key_env: str, token_param: str):
        from openai import AsyncOpenAI

        self.name = name
        self.client = AsyncOpenAI(api_key=_api_key(cfg, default_key_env), base_url=cfg.get("base_url"))
        self.model = cfg["model"]
        self.extra: dict = {}
        if "max_tokens" in cfg:
            self.extra[token_param] = int(cfg["max_tokens"])

    async def _complete(self, system: str, user: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **self.extra,
        )
        return resp.choices[0].message.content or ""


PROVIDERS = ("anthropic", "openai", "openai_compatible")


def build_provider(llm_cfg: dict) -> LLMProvider:
    provider = llm_cfg.get("provider")
    if provider not in PROVIDERS:
        raise SystemExit(f"[llm].provider должен быть одним из: {', '.join(PROVIDERS)}")
    section = llm_cfg.get(provider)
    if not section or "model" not in section:
        raise SystemExit(f"В config.toml нет секции [llm.{provider}] с параметром model")

    if provider == "anthropic":
        return AnthropicProvider(section)
    if provider == "openai":
        return OpenAIProvider(
            section, name="openai", default_key_env="OPENAI_API_KEY", token_param="max_completion_tokens"
        )
    if "base_url" not in section:
        raise SystemExit("В [llm.openai_compatible] нужен base_url")
    return OpenAIProvider(
        section,
        name="openai_compatible",
        default_key_env="OPENAI_COMPATIBLE_API_KEY",
        token_param="max_tokens",
    )
