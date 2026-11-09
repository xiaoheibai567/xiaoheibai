"""End-to-end HTTP acceptance check for the default demo deployment."""

import os
import time

import httpx

headers = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}
with httpx.Client(
    base_url=os.getenv("APP_URL", "http://127.0.0.1:8000"), headers=headers, timeout=45
) as client:
    for _attempt in range(30):
        try:
            if client.get("/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Service failed to become healthy")
    status = client.get("/api/status").json()
    assert status["llm_mode"] == "demo" and status["serial_mode"] == "mock", status
    response = client.post(
        "/api/chat",
        json={
            "question": "查询 STM32F103C8T6 库存，读取协议并打开 LED",
            "allow_control": True,
        },
    )
    response.raise_for_status()
    data = response.json()
    assert data["sources"], data
    assert [t["name"] for t in data["tool_calls"]] == [
        "query_database",
        "read_file",
        "serial_control",
    ], data
    assert all(t["status"] == "success" for t in data["tool_calls"]), data
    assert data["tool_calls"][-1]["result"]["response"] == "LED=ON", data
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    print(f"PASS: document retrieval + three tools + frontend; {data['timings']['total_ms']} ms")
