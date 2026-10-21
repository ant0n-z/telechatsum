"""Единый интерфейс к LLM-провайдерам. Выбор провайдера — в config.toml, секция [llm]."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
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


def _post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    """Синхронный POST с JSON на стандартной библиотеке (без openai/anthropic SDK и pydantic)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "tg-summarizer", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise LLMError(f"HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, cfg: dict):
        self.api_key = _api_key(cfg, "ANTHROPIC_API_KEY")
        self.base_url = str(cfg.get("base_url", "https://api.anthropic.com/v1")).rstrip("/")
        self.model = cfg["model"]
        self.max_tokens = int(cfg.get("max_tokens", 2000))
        self.timeout = float(cfg.get("timeout", 180))

    async def _complete(self, system: str, user: str) -> str:
        data = await asyncio.to_thread(
            _post_json,
            f"{self.base_url}/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            self.timeout,
        )
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OpenAIProvider(LLMProvider):
    """OpenAI и любые OpenAI-совместимые API (OpenRouter, DeepSeek, Groq, Ollama, vLLM...).

    Работает напрямую по HTTP (chat/completions), без пакета openai: он тянет pydantic-core,
    который не собирается, например, в Termux.
    """

    def __init__(self, cfg: dict, *, name: str, default_key_env: str, token_param: str):
        self.name = name
        self.api_key = _api_key(cfg, default_key_env)
        self.base_url = str(cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        self.model = cfg["model"]
        self.timeout = float(cfg.get("timeout", 180))
        self.extra: dict = {}
        if "max_tokens" in cfg:
            self.extra[token_param] = int(cfg["max_tokens"])

    async def _complete(self, system: str, user: str) -> str:
        data = await asyncio.to_thread(
            _post_json,
            f"{self.base_url}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}"},
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                **self.extra,
            },
            self.timeout,
        )
        try:
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"неожиданный формат ответа: {str(data)[:300]}") from e


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
