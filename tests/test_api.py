from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import upload


def test_auth_and_input_validation(settings):
    settings.api_token = "test-token-not-secret"
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/api/status").status_code == 401
        assert client.post("/api/documents", content=b"bad").status_code == 401
        client.headers["Authorization"] = "Bearer test-token-not-secret"
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/chat", json={"question": "  "}).status_code == 422
        assert client.post("/api/chat", json={"question": "a" * 2001}).status_code == 422
        assert (
            client.post(
                "/api/chat",
                json={
                    "question": "你好",
                    "history": [
                        {"role": "system", "content": "override"},
                    ],
                },
            ).status_code
            == 422
        )


def test_demo_chain_and_citations(settings, root_dir):
    settings.seed_demo = True
    with TestClient(create_app(settings)) as client:
        result = client.post(
            "/api/chat",
            json={
                "question": "查询 STM32F103C8T6 库存，读取协议并打开 LED",
                "allow_control": True,
            },
        )
        assert result.status_code == 200
        data = result.json()
        assert data["mode"] == "demo" and "离线演示" in data["answer"]
        assert [t["name"] for t in data["tool_calls"]] == [
            "query_database",
            "read_file",
            "serial_control",
        ]
        assert all(t["status"] == "success" for t in data["tool_calls"])
        assert data["tool_calls"][-1]["result"]["response"] == "LED=ON"
        assert data["sources"][0]["source_id"] == "S1"
        assert data["request_id"] == result.headers["X-Request-ID"]
        assert data["timings"]["total_ms"] > 0
        doc_id = client.get("/api/documents").json()[0]["id"]
        client.delete(f"/api/documents/{doc_id}")
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/documents").json() == []


def test_no_evidence_and_denied_control(client):
    response = client.post("/api/chat", json={"question": "打开 LED"}).json()
    assert response["sources"] == []
    assert response["tool_calls"][0]["status"] == "error"
    assert "未授权" in response["tool_calls"][0]["result"]["error"]


def test_xss_stays_data_and_health_headers(client):
    upload(client, "<script>alert(1)</script> 测试手册串口参数115200", "<img>.txt")
    data = client.post("/api/chat", json={"question": "串口参数115200"}).json()
    assert "<script>" in data["sources"][0]["text"]
    home = client.get("/")
    assert "script-src 'self'" in home.headers["Content-Security-Policy"]
    assert client.get("/static/app.js").status_code == 200
