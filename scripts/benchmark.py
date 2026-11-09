"""Measure complete HTTP round trips. Never substitute demo data for API measurements."""

import argparse
import json
import math
import os
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--question", default="XH-01 的串口波特率和接线要求是什么？")
    parser.add_argument("--target-ms", type=float, default=2000)
    parser.add_argument("--output", default="benchmark.json")
    args = parser.parse_args()
    if args.samples < 1 or args.warmup < 0:
        parser.error("samples must be positive; warmup must be nonnegative")
    headers = (
        {"Authorization": f"Bearer {os.environ['API_TOKEN']}"} if os.getenv("API_TOKEN") else {}
    )
    with httpx.Client(base_url=args.url, timeout=180, headers=headers) as client:
        status_response = client.get("/api/status")
        status_response.raise_for_status()
        status = status_response.json()
        rows = []
        for index in range(args.warmup + args.samples):
            start = time.perf_counter()
            response = client.post("/api/chat", json={"question": args.question})
            elapsed = (time.perf_counter() - start) * 1000
            response.raise_for_status()
            data = response.json()
            if data["status"] != "completed":
                raise RuntimeError(f"Incomplete answer: {data['status']}")
            if index >= args.warmup:
                rows.append(
                    {
                        "http_ms": round(elapsed, 2),
                        **data["timings"],
                        "sources": len(data["sources"]),
                        "tools": len(data["tool_calls"]),
                    }
                )
        latencies = [row["http_ms"] for row in rows]
        result = {
            "measured_at_utc": datetime.now(UTC).isoformat(),
            "mode": status["llm_mode"],
            "embedding_mode": status["embedding_mode"],
            "model": status["model"],
            "documents": status["documents"],
            "chunks": status["chunks"],
            "question": args.question,
            "samples": args.samples,
            "warmup": args.warmup,
            "concurrency": 1,
            "mean_ms": round(statistics.mean(latencies), 2),
            "p50_ms": round(statistics.median(latencies), 2),
            "p95_ms": round(percentile(latencies, 0.95), 2),
            "target_ms": args.target_ms,
            "p95_within_target": percentile(latencies, 0.95) <= args.target_ms,
            "note": "Offline demo; not LLM inference latency."
            if status["llm_mode"] == "demo"
            else "Real API round trips; repeated question uses query embedding cache.",
            "measurements": rows,
        }
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {key: value for key, value in result.items() if key != "measurements"},
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
