from __future__ import annotations

import json

import pytest

from app import main as workbench
from app.ai_provider import DeepSeekMappingProvider, ProviderError, load_deepseek_api_key, parse_json_object
from app.config import Settings


class FakeResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "message": {"content": json.dumps({"steps": []})},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse()


    async def aclose(self) -> None:
        return None


def test_secret_loader_prefers_environment(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("file-only-secret", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-secret")
    assert load_deepseek_api_key(Settings(DEEPSEEK_API_KEY_FILE=key_file)) == "environment-secret"


@pytest.mark.asyncio
async def test_provider_uses_openai_compatible_endpoint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    client = FakeClient()
    provider = DeepSeekMappingProvider(Settings(), client=client)
    output, metadata = await provider.generate_json(
        system_prompt="Return JSON.",
        user_prompt="Propose mappings.",
    )
    assert output == {"steps": []}
    assert metadata["returned_model"] == "deepseek-v4-flash"
    assert client.calls[0]["url"] == "https://api.deepseek.com/chat/completions"
    payload = client.calls[0]["json"]
    assert payload["thinking"] == {"type": "enabled"}
    assert "test-only-secret" not in json.dumps(payload)


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"steps": []}\n```',
        'Here is the requested object:\n{"steps": []}',
    ],
)
def test_provider_json_parser_accepts_safe_wrappers(content: str) -> None:
    assert parse_json_object(content) == {"steps": []}


@pytest.mark.asyncio
async def test_invalid_json_is_retried_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    async def fake_generate(self, *, system_prompt: str, user_prompt: str):  # type: ignore[no-untyped-def]
        calls.append(system_prompt)
        if len(calls) == 1:
            raise ProviderError("invalid_json", retryable=True)
        return {"steps": []}, {"returned_model": "deepseek-flash"}

    monkeypatch.setattr(DeepSeekMappingProvider, "generate_json", fake_generate)
    output, metadata = await workbench.generate_json_with_retry(
        Settings(),
        system_prompt="Return JSON only.",
        user_prompt="{}",
    )
    assert output == {"steps": []}
    assert metadata["retry_count"] == 1
    assert len(calls) == 2
    assert "previous response was not parseable" in calls[1]
