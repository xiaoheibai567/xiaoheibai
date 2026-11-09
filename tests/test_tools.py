import sqlite3

import pytest

from app.tools import Tools


@pytest.fixture
def tools(settings):
    settings.data_dir.mkdir()
    instance = Tools(settings)
    instance.seed()
    return instance


def test_parameterized_inventory_query(tools):
    result = tools.execute(
        "query_database",
        {
            "sql": "SELECT stock, location FROM components WHERE model=?",
            "parameters": ["STM32F103C8T6"],
        },
        False,
    )
    assert result["rows"] == [[24, "A-03"]]
    malicious = tools.query_database("SELECT * FROM components WHERE model=?", ["' OR 1=1 --"])
    assert malicious["rows"] == []


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM components",
        "ATTACH DATABASE 'bad.db' AS other",
        "SELECT load_extension('bad')",
        "SELECT * FROM sqlite_master",
        "SELECT 1; DELETE FROM components",
        "SELECT randomblob(1000000000)",
    ],
)
def test_sql_guard(tools, sql):
    with pytest.raises((ValueError, sqlite3.Error)):
        tools.query_database(sql, [])
    assert len(tools.query_database("SELECT model FROM components", [])["rows"]) == 3


@pytest.mark.parametrize("path", ["../.env", "../../app/config.py", "/etc/passwd", "secret.exe"])
def test_file_boundary(tools, path):
    with pytest.raises((ValueError, OSError)):
        tools.read_file(path)


def test_protocol_and_control_authorization(tools):
    assert "LED_ON" in tools.read_file("device_protocol.md")["content"]
    with pytest.raises(ValueError, match="未授权"):
        tools.execute("serial_control", {"command": "LED_ON"}, False)
    assert tools.execute("serial_control", {"command": "STATUS"}, False)["response"] == "LED=OFF"
    assert tools.execute("serial_control", {"command": "LED_ON"}, True)["response"] == "LED=ON"
    with pytest.raises(ValueError):
        tools.execute("serial_control", {"command": "RESET"}, True)
    with pytest.raises(ValueError):
        tools.execute("serial_control", {"command": "LED_ON", "port": "COM5"}, True)
