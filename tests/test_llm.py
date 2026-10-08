"""OpenAI client wrapper tests — the SDK is exercised against a mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent.llm import LLMConfigurationError, LLMError, OpenAILLM, UnconfiguredLLM, build_llm
from app.agent.planner import PlannerOutput


def _response(text: str) -> dict:
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "model": "test-model",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def _llm(handler) -> tuple[OpenAILLM, httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAILLM("sk-test", "test-model", max_retries=0, http_client=client), client


async def test_openai_llm_uses_responses_structured_outputs() -> None:
    seen: dict = {}
    plan = {
        "summary": "s",
        "steps": [{"description": "d", "tool": None, "requires_approval": False}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_response(json.dumps(plan)))

    llm, client = _llm(handler)
    result = await llm.generate(system="sys", prompt="goal", schema=PlannerOutput)
    await client.aclose()

    assert isinstance(result, PlannerOutput) and result.steps[0].description == "d"
    assert seen["path"].endswith("/responses")
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["instructions"] == "sys"
    assert seen["body"]["text"]["format"]["type"] == "json_schema"
    assert seen["body"]["text"]["format"]["strict"] is True


async def test_openai_llm_invalid_output_raises_llm_error() -> None:
    llm, client = _llm(lambda r: httpx.Response(200, json=_response('{"nope": 1}')))
    with pytest.raises(LLMError):
        await llm.generate(system="s", prompt="p", schema=PlannerOutput)
    await client.aclose()


async def test_openai_llm_auth_error_is_configuration_error() -> None:
    llm, client = _llm(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(LLMConfigurationError, match="API key"):
        await llm.generate(system="s", prompt="p", schema=PlannerOutput)
    await client.aclose()


async def test_openai_llm_server_error_is_llm_error() -> None:
    llm, client = _llm(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))
    with pytest.raises(LLMError):
        await llm.generate(system="s", prompt="p", schema=PlannerOutput)
    await client.aclose()


async def test_build_llm_without_configuration() -> None:
    llm = build_llm(None, None, timeout=5, max_retries=0)
    assert isinstance(llm, UnconfiguredLLM) and not llm.is_configured
    with pytest.raises(LLMConfigurationError, match="OPENAI_API_KEY"):
        await llm.generate(system="s", prompt="p", schema=PlannerOutput)
