import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from pypdf import PdfReader


@dataclass
class Chunk:
    text: str
    page: int
    position: int


def clean_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    if not name or len(name) > 180 or re.search(r"[\x00-\x1f]", name):
        raise ValueError("文件名无效")
    return name


def extract_pages(name: str, content: bytes, max_chars: int) -> list[tuple[int, str]]:
    suffix = Path(name).suffix.lower()
    pages = []
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("请先解密 PDF")
        if len(reader.pages) > 1000:
            raise ValueError("PDF 超过 1000 页限制")
        count = 0
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            count += len(text)
            if count > max_chars:
                raise ValueError("文档提取文本过大")
            pages.append((i, text))
    elif suffix == ".docx":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 50 * 1024 * 1024:
                raise ValueError("Word 解压后超过 50 MB 限制")
        doc = Document(io.BytesIO(content))
        # Word pagination depends on the renderer; page=1 denotes the logical document.
        paragraphs = [p.text for p in doc.paragraphs]
        paragraphs += [" | ".join(c.text for c in row.cells) for t in doc.tables for row in t.rows]
        pages = [(1, "\n".join(paragraphs))]
    elif suffix in {".md", ".txt"}:
        pages = [(1, content.decode("utf-8-sig"))]
    elif suffix == ".doc":
        raise ValueError("旧版 .doc 请先另存为 .docx")
    else:
        raise ValueError("仅支持 PDF、DOCX、TXT、Markdown")
    if sum(len(text) for _, text in pages) > max_chars:
        raise ValueError("文档提取文本过大")
    if not any(text.strip() for _, text in pages):
        raise ValueError("文档没有可提取文本；扫描 PDF 请先 OCR")
    return pages


def split_pages(pages: list[tuple[int, str]], size: int, overlap: int) -> list[Chunk]:
    chunks = []
    for page, raw in pages:
        text = re.sub(r"[ \t]+", " ", raw).strip()
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
                if boundary > start + size // 2:
                    end = boundary + 1
            value = text[start:end].strip()
            if value:
                chunks.append(Chunk(value, page, len(chunks)))
            if end == len(text):
                break
            start = end - overlap
    return chunks
