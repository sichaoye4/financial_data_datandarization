"""DeepSeek OpenAI-compatible adapter for constrained mapping suggestions."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, get_settings


def parse_json_object(content: object) -> dict[str, Any]:
    """Parse a JSON object while tolerating harmless Markdown/prose wrappers."""

    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty provider content")
    text = content.strip()
    candidates = [text]
    if text.startswith("```"):
        first_newline = text.find("\n")
        fenced = text[first_newline + 1 :] if first_newline >= 0 else text[3:]
        if fenced.rstrip().endswith("```"):
            fenced = fenced.rstrip()[:-3]
        candidates.append(fenced.strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[index:])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError("provider content did not contain a JSON object")


class ProviderError(RuntimeError):
    """A redacted provider failure safe to surface to the API client."""

    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code


def load_deepseek_api_key(settings: Settings | None = None) -> str | None:
    """Load direct injection first, then the configured backend-only file."""

    active = settings or get_settings()
    direct = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if direct:
        return direct
    path: Path | None = active.deepseek_api_key_file
    if path is None:
        configured_path = os.getenv("DEEPSEEK_API_KEY_FILE", "").strip()
        path = Path(configured_path) if configured_path else None
    if path is None:
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


class DeepSeekMappingProvider:
    """Small async HTTP client matching Football Legend's provider boundary."""

    provider_name = "deepseek"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._timeout_seconds = timeout_seconds

    def request_payload(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.settings.deepseek_max_tokens,
            "stream": False,
            "thinking": {"type": self.settings.deepseek_thinking},
        }
        return payload

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        api_key = load_deepseek_api_key(self.settings)
        if not api_key:
            raise ProviderError("not_configured")

        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.AsyncClient(timeout=self._timeout_seconds)
        assert client is not None
        started = time.perf_counter()
        try:
            response = await client.post(
                f"{self.settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=self.request_payload(system_prompt, user_prompt),
            )
        except httpx.TimeoutException as error:
            raise ProviderError("timeout", retryable=True) from error
        except httpx.HTTPError as error:
            raise ProviderError("network", retryable=True) from error
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code >= 400:
            raise ProviderError(
                f"http_{response.status_code}",
                retryable=response.status_code in {429, 500, 503},
                status_code=response.status_code,
            )
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            output = parse_json_object(content)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProviderError("invalid_json") from error
        if not isinstance(output, dict):
            raise ProviderError("invalid_output")
        usage = payload.get("usage") if isinstance(payload, dict) else None
        metadata = {
            "provider": self.provider_name,
            "requested_model": self.settings.deepseek_model,
            "returned_model": payload.get("model"),
            "finish_reason": choice.get("finish_reason"),
            "latency_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "usage": usage if isinstance(usage, dict) else {},
        }
        return output, metadata
