import io
import zipfile

from docx import Document
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas

from app.main import create_app
from tests.conftest import upload


def test_incremental_update_retrieval_and_delete(client):
    first = upload(client, "星河实验板的串口波特率是115200，串口控制使用STATUS指令。")
    assert first.status_code == 200
    first = first.json()
    assert first["status"] == "created" and first["embedded_chunks"] == 1
    doc_id = first["document"]["id"]
    repeated = upload(client, "星河实验板的串口波特率是115200，串口控制使用STATUS指令。")
    assert repeated.json()["status"] == "unchanged"
    update = upload(client, "星河实验板的串口波特率是9600，串口控制使用STATUS指令。")
    assert update.json()["status"] == "updated"
    assert update.json()["document"]["id"] == doc_id
    sources = client.post("/api/search", json={"query": "串口波特率"}).json()["sources"]
    assert len(sources) == 1 and "9600" in sources[0]["text"]
    assert "115200" not in sources[0]["text"]
    assert client.delete(f"/api/documents/{doc_id}").status_code == 200
    assert client.get("/api/status").json()["chunks"] == 0
    assert client.post("/api/search", json={"query": "串口波特率"}).json()["sources"] == []
    assert client.delete(f"/api/documents/{doc_id}").status_code == 404


def test_reuse_vectors_and_persistence(client, settings):
    # Identical chunks can be reused even when they occur in another document.
    text = "文档增量更新会复用已存在的向量。" * 20
    assert upload(client, text).json()["embedded_chunks"] > 0
    second = upload(client, text, "copy.md").json()
    assert second["embedded_chunks"] == 0 and second["reused_chunks"] > 0
    with TestClient(create_app(settings)) as restarted:
        assert len(restarted.get("/api/documents").json()) == 2
        assert restarted.post("/api/search", json={"query": "增量更新"}).json()["sources"]


def test_docx_paragraph_and_table(client):
    doc = Document()
    doc.add_paragraph("开发板串口配置：115200 baud")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "LED_ON"
    table.cell(0, 1).text = "点亮指示灯"
    output = io.BytesIO()
    doc.save(output)
    response = upload(client, output.getvalue(), "说明书.docx")
    assert response.status_code == 200
    sources = client.post("/api/search", json={"query": "LED_ON 点亮"}).json()["sources"]
    assert "LED_ON | 点亮指示灯" in sources[0]["text"]


def test_pdf_keeps_page_number(client):
    output = io.BytesIO()
    pdf = Canvas(output)
    pdf.drawString(50, 750, "Power supply: 5V USB")
    pdf.showPage()
    pdf.drawString(50, 750, "UART baudrate 115200 serial protocol STATUS LED_ON")
    pdf.save()
    assert upload(client, output.getvalue(), "manual.pdf").status_code == 200
    hits = client.post("/api/search", json={"query": "UART baudrate 115200"}).json()["sources"]
    assert hits[0]["page"] == 2


def test_rejected_document_does_not_destroy_previous_revision(client):
    upload(client, "原始手册内容：串口波特率115200。", "manual.docx.txt")
    invalid = upload(client, b"\xff\xfe\x00", "manual.docx.txt")
    assert invalid.status_code == 422
    assert client.get("/api/status").json()["chunks"] == 1


def test_unsupported_empty_corrupt_and_oversize(client, settings):
    for name, data in [
        ("old.doc", b"old format"),
        ("empty.txt", b""),
        ("bad.pdf", b"not a pdf"),
        ("bad.docx", b"not a docx"),
    ]:
        assert upload(client, data, name).status_code == 422
    settings.max_upload_mb = 1
    assert upload(client, b"x" * (1024 * 1024 + 1)).status_code == 413
    huge_body = client.post("/api/documents", content=b"x" * (2 * 1024 * 1024 + 1))
    assert huge_body.status_code == 413


def test_docx_zip_bomb_limit(client):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("document.xml", "x" * (51 * 1024 * 1024))
    result = upload(client, data.getvalue(), "huge.docx")
    assert result.status_code == 422 and "50 MB" in result.json()["detail"]


def test_normalizes_filename(client):
    response = upload(client, "中文手册串口内容", "../../manual.txt")
    assert response.json()["document"]["name"] == "manual.txt"


def test_changed_file_reuses_unchanged_chunks(settings):
    settings.chunk_size = 100
    settings.chunk_overlap = 0
    with TestClient(create_app(settings)) as client:
        first = upload(client, "A" * 80 + "\n" + "B" * 80).json()
        assert first["embedded_chunks"] == 2
        update = upload(client, "A" * 80 + "\n" + "C" * 80).json()
        assert update["status"] == "updated"
        assert update["embedded_chunks"] == 1
        assert update["reused_chunks"] == 1
        assert client.get("/api/status").json()["chunks"] == 2
