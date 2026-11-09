import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Literal

import serial
from pydantic import BaseModel, ConfigDict, Field


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QueryInput(ToolInput):
    sql: str = Field(min_length=1, max_length=2000)
    parameters: list[str | int | float | None] = Field(default_factory=list, max_length=30)


class FileInput(ToolInput):
    path: str = Field(min_length=1, max_length=240)


class SerialInput(ToolInput):
    command: Literal["STATUS", "LED_ON", "LED_OFF"]


TOOL_MODELS = {"query_database": QueryInput, "read_file": FileInput, "serial_control": SerialInput}
DESCRIPTIONS = {
    "query_database": "只读查询器件库存。表 components(model TEXT, stock INTEGER, location TEXT)。"
    "使用 SELECT 和 ? 占位符，最多返回 50 行。",
    "read_file": "读取工具目录内 UTF-8 文本文件，例如 device_protocol.md。禁止绝对路径及目录穿越。",
    "serial_control": "向配置设备发送 STATUS、LED_ON 或 LED_OFF。"
    "改变状态需本轮 allow_control=true。",
}
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": DESCRIPTIONS[name],
            "parameters": model.model_json_schema(),
        },
    }
    for name, model in TOOL_MODELS.items()
]


class Tools:
    def __init__(self, settings):
        self.settings = settings
        self.db_path = settings.data_dir / "inventory.db"
        self.serial_lock = threading.Lock()
        self.led = False

    def seed(self):
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS components "
                    "(model TEXT PRIMARY KEY, stock INTEGER, location TEXT)"
                )
                db.executemany(
                    "INSERT OR IGNORE INTO components VALUES (?, ?, ?)",
                    [
                        ("STM32F103C8T6", 24, "A-03"),
                        ("ESP32-WROOM-32", 16, "B-02"),
                        ("LED-5MM", 120, "C-01"),
                    ],
                )
        finally:
            db.close()

    def query_database(self, sql: str, parameters: list) -> dict:
        if not sql.lstrip().lower().startswith("select"):
            raise ValueError("仅允许 SELECT 只读查询")
        db = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            db.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

            def authorize(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and arg1 == "components":
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {
                    "count",
                    "sum",
                    "avg",
                    "min",
                    "max",
                    "lower",
                    "upper",
                    "like",
                    "coalesce",
                }:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            db.set_authorizer(authorize)
            cursor = db.execute(sql, parameters)
            rows = cursor.fetchmany(51)
            result = {
                "columns": [c[0] for c in cursor.description],
                "rows": [list(row) for row in rows[:50]],
                "truncated": len(rows) > 50,
            }
            if len(json.dumps(result, ensure_ascii=False)) > 12000:
                raise ValueError("查询返回过大，请缩小查询范围")
            return result
        finally:
            db.close()

    def read_file(self, path: str) -> dict:
        root = self.settings.tool_files_dir.resolve()
        requested = Path(path)
        if requested.is_absolute() or ".." in requested.parts:
            raise ValueError("文件路径必须位于工具目录内")
        resolved = (root / requested).resolve()
        if not resolved.is_relative_to(root) or resolved.suffix.lower() not in {
            ".md",
            ".txt",
            ".json",
        }:
            raise ValueError("文件路径或类型不允许")
        with resolved.open("rb") as file:
            content = file.read(12001)
        if len(content) > 12000:
            raise ValueError("工具文件超过 12 KB 限制")
        return {"path": path, "content": content.decode("utf-8-sig")}

    def serial_control(self, command: str, allow_control: bool) -> dict:
        if command not in {"STATUS", "LED_ON", "LED_OFF"}:
            raise ValueError("串口命令不在允许列表")
        if command != "STATUS" and not allow_control:
            raise ValueError("本轮未授权改变设备状态，请勾选允许设备控制")
        with self.serial_lock:
            if self.settings.serial_mode == "mock":
                if command != "STATUS":
                    self.led = command == "LED_ON"
                return {
                    "mode": "mock",
                    "command": command,
                    "response": "LED=ON" if self.led else "LED=OFF",
                }
            with serial.Serial(
                self.settings.serial_port,
                self.settings.serial_baudrate,
                timeout=1,
                write_timeout=1,
            ) as port:
                port.reset_input_buffer()
                port.write((command + "\n").encode("ascii"))
                port.flush()
                response = port.read_until(b"\n", 256).decode("utf-8", errors="replace").strip()
                if not response:
                    raise ValueError("设备未确认；命令可能已发送，请先查询 STATUS，勿自动重试")
                return {"mode": "real", "command": command, "response": response}

    def execute(self, name: str, arguments: dict, allow_control: bool) -> dict:
        model = TOOL_MODELS.get(name)
        if model is None:
            raise ValueError("未知工具")
        parsed = model.model_validate(arguments).model_dump()
        if name == "serial_control":
            return self.serial_control(**parsed, allow_control=allow_control)
        return getattr(self, name)(**parsed)
