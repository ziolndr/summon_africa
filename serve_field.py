#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
FIELD = ROOT / "field"
SEED = ROOT / "data/seed.json"
DEFAULT_ARBITER = "https://api.arbiter.traut.ai/public/embed"
EMBED_URL = os.getenv("ARBITER_EMBED_URL", DEFAULT_ARBITER)
DIMENSION = 72


def extract_vector(payload: Any) -> np.ndarray:
    if isinstance(payload, dict):
        for key in ("vectors", "embeddings", "data", "results"):
            if key not in payload:
                continue
            value = payload[key]
            if key == "data" and isinstance(value, list) and value and isinstance(value[0], dict):
                value = [value[0].get("embedding") or value[0].get("vector")]
            array = np.asarray(value, dtype=np.float32)
            return array[0] if array.ndim > 1 else array
        if "embedding" in payload:
            return np.asarray(payload["embedding"], dtype=np.float32)
    if isinstance(payload, list):
        array = np.asarray(payload, dtype=np.float32)
        return array[0] if array.ndim > 1 else array
    raise ValueError("no embedding")


def embed_query(query: str) -> np.ndarray:
    last: Exception | None = None
    for body in (
        {"texts": [query], "use_freq": True},
        {"texts": [query]},
        {"input": [query]},
        {"text": query},
        {"input": query},
    ):
        try:
            response = requests.post(EMBED_URL, json=body, timeout=60)
            if response.ok:
                vector = extract_vector(response.json())
                norm = float(np.linalg.norm(vector))
                return vector / (norm or 1)
            last = RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            last = exc
    raise RuntimeError(str(last or "embedding failed"))


def hash_features(text: str) -> list[tuple[str, float]]:
    normalized = unicodedata.normalize("NFKD", text.lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9']+", normalized)
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "for", "with", "that", "this", "is", "it",
        "format", "description", "themes", "creator", "country", "languages", "video", "film", "movie",
    }
    output: list[tuple[str, float]] = []
    for token in tokens:
        if len(token) > 2 and token not in stop:
            output.append((f"w:{token}", 1.0 + min(len(token), 12) / 24))
    collapsed = re.sub(r"\s+", " ", normalized).strip()
    for index in range(max(0, len(collapsed) - 3)):
        gram = collapsed[index : index + 4]
        if gram.strip():
            output.append((f"c:{gram}", 0.16))
    return output


def hash_vector(text: str, dimension: int = DIMENSION) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    for feature, weight in hash_features(text):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "little") % dimension
        sign = 1.0 if digest[8] & 1 else -1.0
        vector[index] += sign * float(weight)
    norm = float(np.linalg.norm(vector))
    return vector / (norm or 1)


class State:
    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.manifest: dict[str, Any] = {
            "name": "SUMMON AFRICA",
            "ready": False,
            "count": 0,
            "dimension": DIMENSION,
            "dim": DIMENSION,
            "playable": 0,
            "embedding_method": "local-hash-72d",
        }
        self.meta: list[dict[str, Any]] = []
        self.vectors: np.ndarray | None = None

        manifest_path = FIELD / "manifest.json"
        metadata_path = FIELD / "metadata.jsonl"
        vectors_path = FIELD / "vectors.f32"
        if manifest_path.exists():
            self.manifest.update(json.loads(manifest_path.read_text("utf-8")))
        if metadata_path.exists():
            self.meta = [
                json.loads(line)
                for line in metadata_path.read_text("utf-8").splitlines()
                if line.strip()
            ]
        elif SEED.exists():
            payload = json.loads(SEED.read_text("utf-8"))
            self.meta = payload if isinstance(payload, list) else []

        dimension = int(self.manifest.get("dimension") or self.manifest.get("dim") or DIMENSION)
        if vectors_path.exists() and self.meta:
            array = np.fromfile(vectors_path, dtype=np.float32)
            if array.size == len(self.meta) * dimension:
                self.vectors = array.reshape(len(self.meta), dimension)
        self.manifest.update(
            {
                "count": len(self.meta),
                "playable": len(self.meta),
                "ready": bool(self.meta),
                "dimension": dimension,
                "dim": dimension,
                "units": int(self.manifest.get("units") or len(self.meta)),
                "use_freq": self.manifest.get("use_freq") is not False,
            }
        )

    def query_vector(self, query: str) -> np.ndarray:
        method = str(self.manifest.get("embedding_method") or "")
        if method.startswith("arbiter"):
            vector = embed_query(query)
            if vector.size != int(self.manifest.get("dimension") or DIMENSION):
                raise RuntimeError("query embedding dimension mismatch")
            return vector.astype(np.float32, copy=False)
        return hash_vector(query, int(self.manifest.get("dimension") or DIMENSION))


STATE = State()


def normalized_type(value: Any) -> str:
    return str(value or "").strip().lower()


def search(query: str, media_type: str = "all", limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    indexes = [
        index
        for index, row in enumerate(STATE.meta)
        if media_type == "all" or normalized_type(row.get("type")) == media_type
    ]
    if not indexes:
        return []

    if STATE.vectors is None:
        return [dict(STATE.meta[index], score=0.0) for index in indexes[offset : offset + limit]]

    vector = STATE.query_vector(query)
    scores = STATE.vectors[indexes] @ vector
    order = np.argsort(-scores)
    selected = order[offset : offset + limit]
    return [
        dict(STATE.meta[indexes[int(local_index)]], score=float(scores[int(local_index)]))
        for local_index in selected
    ]


class App(BaseHTTPRequestHandler):
    server_version = "SummonAfrica/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")

    def send_json(self, payload: Any, status: int = 200, head: bool = False) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.cors()
        self.end_headers()
        if not head:
            self.wfile.write(raw)

    def static_target(self, path: str) -> Path | None:
        clean = "index.html" if path == "/" else posixpath.normpath(path).lstrip("/")
        target = (ROOT / clean).resolve()
        if target != ROOT and ROOT not in target.parents:
            return None
        return target

    def send_static(self, path: str, head: bool = False) -> None:
        target = self.static_target(path)
        if target is None:
            self.send_error(403)
            return
        if not target.exists() or not target.is_file():
            self.send_error(404)
            return
        raw = b"" if head else target.read_bytes()
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-cache" if target.suffix in {".html", ".json"} else "public, max-age=3600")
        self.cors()
        self.end_headers()
        if not head:
            self.wfile.write(raw)

    def manifest_payload(self) -> dict[str, Any]:
        return dict(STATE.manifest)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/field/v1/manifest":
            self.send_json(self.manifest_payload(), head=True)
            return
        if parsed.path == "/field/health":
            self.send_json({"ok": True, "count": len(STATE.meta), "vectors": STATE.vectors is not None}, head=True)
            return
        self.send_static(parsed.path, head=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/field/v1/manifest":
            self.send_json(self.manifest_payload())
            return
        if parsed.path == "/field/v1/latest":
            limit = min(200, max(1, int((params.get("limit") or [50])[0])))
            self.send_json({"results": STATE.meta[:limit], "items": STATE.meta[:limit], "count": len(STATE.meta)})
            return
        if parsed.path == "/field/health":
            self.send_json(
                {
                    "ok": True,
                    "count": len(STATE.meta),
                    "vectors": STATE.vectors is not None,
                    "embedding_method": STATE.manifest.get("embedding_method"),
                }
            )
            return
        if parsed.path == "/field/reload":
            STATE.reload()
            self.send_json(self.manifest_payload())
            return
        self.send_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/field/v1/search":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json({"error": "invalid json"}, 400)
            return

        query = str(body.get("text") or body.get("query") or body.get("q") or "").strip()
        media_type = normalized_type(body.get("type") or "all")
        limit = min(200, max(1, int(body.get("k") or body.get("limit") or body.get("top_k") or 100)))
        offset = max(0, int(body.get("offset") or 0))
        started = time.perf_counter()
        if not query:
            results = STATE.meta[offset : offset + limit]
        else:
            try:
                results = search(query, media_type, limit, offset)
            except Exception as exc:
                self.send_json({"error": str(exc), "results": []}, 503)
                return

        payload = {
            "results": results,
            "items": results,
            "count": len(STATE.meta),
            "query": query,
            "type": media_type,
            "offset": offset,
            "k": limit,
            "dim": int(STATE.manifest.get("dimension") or DIMENSION),
            "dimension": int(STATE.manifest.get("dimension") or DIMENSION),
            "use_freq": STATE.manifest.get("use_freq") is not False,
            "units": int(STATE.manifest.get("units") or len(STATE.meta)),
            "sources": STATE.manifest.get("sources") or {},
            "updated_at": STATE.manifest.get("updated_at"),
            "ms": round((time.perf_counter() - started) * 1000, 1),
        }
        self.send_json(payload)


def main() -> int:
    global FIELD, EMBED_URL, STATE
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--field-dir", default=str(FIELD))
    parser.add_argument("--embed-url", default=EMBED_URL)
    args = parser.parse_args()
    FIELD = Path(args.field_dir)
    EMBED_URL = args.embed_url
    STATE = State()
    print(
        f"SUMMON AFRICA · http://{args.host}:{args.port} · "
        f"{len(STATE.meta):,} playable records · {STATE.manifest.get('embedding_method')}"
    )
    ThreadingHTTPServer((args.host, args.port), App).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
