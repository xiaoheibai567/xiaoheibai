import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import ProviderError, Providers
from app.store import Store
from tests.conftest import upload


def tool_call(name, arguments, call_id):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_real_protocol_multi_round_tool_chain(settings):
    settings.llm_mode = "api"
    settings.llm_api_key = "test-key"
    messages_seen = []
    replies = [
        tool_call(
            "query_database",
            {
                "sql": "SELECT stock FROM components WHERE model=?",
                "parameters": ["STM32F103C8T6"],
            },
            "call1",
        ),
        tool_call("read_file", {"path": "device_protocol.md"}, "call2"),
        tool_call("serial_control", {"command": "LED_ON"}, "call3"),
        {"role": "assistant", "content": "库存24个；已按协议点亮模拟LED。[S1]"},
    ]

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert len(payload["tools"]) == 3
        messages_seen.append(payload["messages"])
        return httpx.Response(200, json={"choices": [{"message": replies.pop(0)}]})

    provider = Providers(settings, httpx.MockTransport(handler))
    with TestClient(create_app(settings, provider)) as client:
        client.app.state.tools.seed()
        upload(client, "实验板LED控制串口协议支持LED_ON。")
        data = client.post(
            "/api/chat",
            json={
                "question": "查库存，读协议，然后打开LED",
                "allow_control": True,
            },
        ).json()
        assert data["status"] == "completed" and "库存24" in data["answer"]
        assert [t["name"] for t in data["tool_calls"]] == [
            "query_database",
            "read_file",
            "serial_control",
        ]
        assert len([m for m in messages_seen[-1] if m["role"] == "tool"]) == 3
        assert json.loads(messages_seen[1][-1]["content"])["rows"] == [[24]]
        assert data["tool_calls"][-1]["result"]["mode"] == "mock"


def test_tool_errors_can_be_recovered_by_model(settings):
    settings.llm_mode = "api"
    settings.llm_api_key = "test-key"
    replies = [
        tool_call("read_file", {"path": "../.env"}, "bad"),
        {"role": "assistant", "content": "该文件访问被拒绝。"},
    ]

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": replies.pop(0)}]})

    with TestClient(
        create_app(settings, Providers(settings, httpx.MockTransport(handler)))
    ) as client:
        data = client.post("/api/chat", json={"question": "读取配置"}).json()
        assert data["status"] == "completed"
        assert data["tool_calls"][0]["status"] == "error"


def test_no_duplicate_execution_and_loop_limit(settings):
    settings.llm_mode = "api"
    settings.llm_api_key = "test-key"

    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": tool_call(
                            "serial_control",
                            {"command": "LED_ON"},
                            "repeated",
                        )
                    }
                ]
            },
        )

    with TestClient(
        create_app(settings, Providers(settings, httpx.MockTransport(handler)))
    ) as client:
        result = client.post("/api/chat", json={"question": "打开LED", "allow_control": True})
        data = result.json()
        assert data["status"] == "limit_reached" and len(data["tool_calls"]) == 1


def test_upstream_error_preserves_executed_actions(settings):
    settings.llm_mode = "api"
    settings.llm_api_key = "test-key"
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": tool_call(
                                "serial_control",
                                {"command": "LED_ON"},
                                "on",
                            )
                        }
                    ]
                },
            )
        return httpx.Response(500, text="private upstream diagnostic and credential")

    with TestClient(
        create_app(settings, Providers(settings, httpx.MockTransport(handler)))
    ) as client:
        data = client.post("/api/chat", json={"question": "打开LED", "allow_control": True}).json()
        assert data["status"] == "upstream_error"
        assert data["tool_calls"][0]["result"]["response"] == "LED=ON"
        assert "private" not in json.dumps(data)


def test_embedding_batches_indexes_and_cache(settings):
    settings.embedding_mode = "api"
    settings.embedding_api_key = "test-key"
    settings.embedding_dimensions = 8
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [float(i + 1)] * 8}
                    for i in reversed(range(len(payload["input"])))
                ]
            },
        )

    async def scenario():
        provider = Providers(settings, httpx.MockTransport(handler))
        try:
            vectors = await provider.embed(["first", "second", "first"])
            assert vectors == [[1.0] * 8, [2.0] * 8, [1.0] * 8]
            await provider.embed(["first"])
            assert len(calls) == 1
        finally:
            await provider.close()

    asyncio.run(scenario())


def test_bad_embedding_dimensions_and_index_mismatch(settings):
    settings.embedding_mode = "api"
    settings.embedding_api_key = "test-key"

    async def scenario():
        for body in [
            {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
            {"data": [{"index": 1, "embedding": [0.1] * 1536}]},
        ]:
            provider = Providers(
                settings,
                httpx.MockTransport(
                    lambda request, body=body: httpx.Response(200, json=body),
                ),
            )
            try:
                with pytest.raises(ProviderError):
                    await provider.embed(["text"])
            finally:
                await provider.close()

    asyncio.run(scenario())


def test_embedding_configuration_mismatch(settings):
    Store(settings)
    settings.embedding_mode = "api"
    with pytest.raises(ValueError, match="不一致"):
        Store(settings)
