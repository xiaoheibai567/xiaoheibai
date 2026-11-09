import hashlib
import math
import re
from collections import OrderedDict

import httpx

from app.config import Settings


class ProviderError(Exception):
    """An upstream failed; never expose the response body or credentials."""


class Providers:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=settings.request_timeout, transport=transport)
        self.cache: OrderedDict[str, list[float]] = OrderedDict()

    async def close(self):
        await self.client.aclose()

    async def post(self, base: str, key: str, endpoint: str, payload: dict) -> dict:
        try:
            response = await self.client.post(
                base.rstrip("/") + endpoint,
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Deliberately do not automatically retry requests that could trigger tools.
            raise ProviderError("模型服务调用失败，请检查模型配置、额度和网络") from exc

    @staticmethod
    def hash_embedding(text: str, dimensions: int) -> list[float]:
        text = re.sub(r"\s+", "", text.lower())
        tokens = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
        tokens += [text[i : i + 2] for i in range(max(0, len(text) - 1))]
        vector = [0.0] * dimensions
        for token in tokens:
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % dimensions
            vector[index] += 1 if digest[4] % 2 else -1
        norm = math.sqrt(sum(v * v for v in vector))
        if not norm:
            vector[0] = 1
            norm = 1
        return [v / norm for v in vector]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        cfg = self.settings
        missing = list(dict.fromkeys(t for t in texts if t not in self.cache))
        generated = {}
        for offset in range(0, len(missing), 32):
            batch = missing[offset : offset + 32]
            if cfg.embedding_mode == "hash":
                vectors = [self.hash_embedding(t, cfg.vector_dimensions) for t in batch]
            else:
                data = await self.post(
                    cfg.embedding_base_url,
                    cfg.embedding_api_key,
                    "/embeddings",
                    {"model": cfg.embedding_model, "input": batch, "encoding_format": "float"},
                )
                try:
                    items = sorted(data["data"], key=lambda item: item["index"])
                    if [item["index"] for item in items] != list(range(len(batch))):
                        raise ValueError("invalid indexes")
                    vectors = [item["embedding"] for item in items]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProviderError("embedding 返回格式无效") from exc
            if len(vectors) != len(batch):
                raise ProviderError("embedding 返回数量不匹配")
            for text, vector in zip(batch, vectors, strict=True):
                try:
                    valid = (
                        len(vector) == cfg.vector_dimensions
                        and all(math.isfinite(v) for v in vector)
                        and sum(v * v for v in vector) > 0
                    )
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    raise ProviderError("embedding 维度或数值无效，请检查 EMBEDDING_DIMENSIONS")
                generated[text] = vector
        result = [generated[t] if t in generated else self.cache[t] for t in texts]
        self.cache.update(generated)
        while len(self.cache) > 2048:
            self.cache.popitem(last=False)
        return result

    async def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        cfg = self.settings
        data = await self.post(
            cfg.llm_base_url,
            cfg.llm_api_key,
            "/chat/completions",
            {
                "model": cfg.llm_model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "max_tokens": 800,
            },
        )
        try:
            message = data["choices"][0]["message"]
            if message.get("role") != "assistant":
                raise ValueError("invalid assistant role")
            return {
                key: message[key] for key in ("role", "content", "tool_calls") if key in message
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("模型返回格式无效") from exc
