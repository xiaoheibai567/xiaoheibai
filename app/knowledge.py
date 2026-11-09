import asyncio
import hashlib

import sqlite_vec

from app.documents import clean_filename, extract_pages, split_pages
from app.store import text_hash


class Knowledge:
    def __init__(self, settings, store, providers):
        self.settings = settings
        self.store = store
        self.providers = providers
        self.write_lock = asyncio.Lock()

    async def ingest(self, name: str, content: bytes) -> dict:
        cfg = self.settings
        name = clean_filename(name)
        if not content or len(content) > cfg.max_upload_mb * 1024 * 1024:
            raise ValueError("文件为空或超过上传大小限制")
        digest = hashlib.sha256(content).hexdigest()
        async with self.write_lock:
            existing = await asyncio.to_thread(self.store.find, name)
            if existing and existing["sha256"] == digest:
                return {
                    "status": "unchanged",
                    "document": existing,
                    "embedded_chunks": 0,
                    "reused_chunks": 0,
                }
            try:
                pages = await asyncio.to_thread(
                    extract_pages,
                    name,
                    content,
                    cfg.max_extracted_chars,
                )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError("文档解析失败，请确认文件未损坏且格式正确") from exc
            chunks = split_pages(pages, cfg.chunk_size, cfg.chunk_overlap)
            if len(chunks) > cfg.max_chunks:
                raise ValueError("文档切片数量超过限制")
            vectors = await asyncio.to_thread(self.store.existing_vectors, chunks)
            missing = {
                text_hash(c.text): c.text for c in chunks if text_hash(c.text) not in vectors
            }
            embedded = await self.providers.embed(list(missing.values()))
            for key, vector in zip(missing, embedded, strict=True):
                vectors[key] = sqlite_vec.serialize_float32(vector)
            document = await asyncio.to_thread(
                self.store.replace,
                name,
                digest,
                len(content),
                chunks,
                vectors,
            )
            return {
                "status": "updated" if existing else "created",
                "document": document,
                "embedded_chunks": len(missing),
                "reused_chunks": sum(text_hash(c.text) not in missing for c in chunks),
            }

    async def delete(self, doc_id: str) -> bool:
        async with self.write_lock:
            return await asyncio.to_thread(self.store.delete, doc_id)

    async def search(self, query: str, top_k: int) -> list[dict]:
        vector = (await self.providers.embed([query]))[0]
        return await asyncio.to_thread(self.store.search, vector, top_k)
