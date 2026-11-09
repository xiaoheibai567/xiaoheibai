import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import sqlite_vec

from app.config import Settings
from app.documents import Chunk


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    """Metadata and vec0 vectors share one transaction and one persistent database."""

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "knowledge.db"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
                    updated_at TEXT NOT NULL, size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    text TEXT NOT NULL, text_hash TEXT NOT NULL,
                    page INTEGER NOT NULL, position INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
                CREATE INDEX IF NOT EXISTS chunks_hash ON chunks(text_hash);
            """)
            row = db.execute("SELECT value FROM config WHERE key='embedding'").fetchone()
            if row and row[0] != settings.embedding_signature:
                raise ValueError(
                    "Embedding 配置与已有索引不一致。请使用新的 DATA_DIR 并重新导入文档。"
                )
            db.execute(
                "INSERT OR IGNORE INTO config VALUES ('embedding', ?)",
                (settings.embedding_signature,),
            )
            db.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0("
                f"chunk_id INTEGER PRIMARY KEY, "
                f"embedding FLOAT[{settings.vector_dimensions}] distance_metric=cosine)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list_documents(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("""
                SELECT d.*, COUNT(c.id) AS chunks FROM documents d
                LEFT JOIN chunks c ON c.document_id=d.id GROUP BY d.id
                ORDER BY d.updated_at DESC
            """)
            ]

    def find(self, name: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE name=?", (name,)).fetchone()
            return dict(row) if row else None

    def existing_vectors(self, chunks: list[Chunk]) -> dict[str, bytes]:
        hashes = list({text_hash(c.text) for c in chunks})
        result = {}
        with self.connect() as db:
            for offset in range(0, len(hashes), 500):
                batch = hashes[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                for row in db.execute(
                    "SELECT c.text_hash, v.embedding FROM chunks c "
                    "JOIN vectors v ON v.chunk_id=c.id "
                    f"WHERE c.text_hash IN ({placeholders})",
                    batch,
                ):
                    result[row[0]] = row[1]
        return result

    def replace(self, name, sha256, size, chunks, vectors) -> dict:
        with self.connect() as db:
            # The first statement takes the write lock before looking up the previous revision.
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id FROM documents WHERE name=?", (name,)).fetchone()
            doc_id = previous[0] if previous else uuid.uuid4().hex
            if previous:
                db.execute(
                    "DELETE FROM vectors WHERE chunk_id IN "
                    "(SELECT id FROM chunks WHERE document_id=?)",
                    (doc_id,),
                )
                db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
            updated_at = datetime.now(UTC).isoformat()
            db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET sha256=excluded.sha256, "
                "updated_at=excluded.updated_at, size=excluded.size",
                (doc_id, name, sha256, updated_at, size),
            )
            for chunk in chunks:
                digest = text_hash(chunk.text)
                cursor = db.execute(
                    "INSERT INTO chunks(document_id, text, text_hash, page, position) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, chunk.text, digest, chunk.page, chunk.position),
                )
                db.execute(
                    "INSERT INTO vectors(chunk_id, embedding) VALUES (?, ?)",
                    (cursor.lastrowid, vectors[digest]),
                )
        return {
            "id": doc_id,
            "name": name,
            "sha256": sha256,
            "size": size,
            "updated_at": updated_at,
            "chunks": len(chunks),
        }

    def delete(self, doc_id: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM vectors WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id=?)",
                (doc_id,),
            )
            return db.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount > 0

    def search(self, vector: list[float], top_k: int) -> list[dict]:
        with self.connect() as db:
            matches = db.execute(
                """
                WITH nearest AS (
                    SELECT chunk_id, distance FROM vectors WHERE embedding MATCH ? AND k=?
                )
                SELECT c.id AS chunk_id, c.document_id, d.name, c.text, c.page, c.position,
                       1 - n.distance AS score
                FROM nearest n JOIN chunks c ON c.id=n.chunk_id
                JOIN documents d ON d.id=c.document_id ORDER BY n.distance
            """,
                (sqlite_vec.serialize_float32(vector), top_k),
            ).fetchall()
            return [dict(row) for row in matches if row["score"] >= self.settings.min_score]

    def stats(self):
        with self.connect() as db:
            return {
                "documents": db.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            }
