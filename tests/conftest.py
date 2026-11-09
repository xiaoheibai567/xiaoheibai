from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import ROOT, create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        seed_demo=False,
        tool_files_dir=ROOT / "examples" / "files",
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def upload(client, text, name="manual.txt"):
    content = text.encode() if isinstance(text, str) else text
    return client.post("/api/documents", files={"file": (name, content)})


@pytest.fixture
def root_dir() -> Path:
    return ROOT
