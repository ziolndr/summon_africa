#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_ARBITER = "https://api.arbiter.traut.ai/public/embed"
DEFAULT_TMDB_KEY = "1616a31a4f3bbcb387e74cbe0ae3c7b6"
TMDB_BASE = "https://api.themoviedb.org/3"
DIMENSION = 72


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def load_records(paths: Iterable[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        if path.suffix == ".json":
            payload = json.loads(path.read_text("utf-8"))
            if isinstance(payload, list):
                records.extend(item for item in payload if isinstance(item, dict))
        else:
            records.extend(read_jsonl(path))

    dedup: dict[str, dict[str, Any]] = {}
    for raw in records:
        row = dict(raw)
        provider = str(row.get("provider") or "").strip().lower()
        provider_id = str(row.get("provider_id") or row.get("providerId") or "").strip()
        if not provider or not provider_id or row.get("embeddable") is False:
            continue
        key = str(row.get("id") or f"{provider}:{provider_id}")
        row["id"] = key
        row["provider"] = provider
        row["provider_id"] = provider_id
        dedup[key] = row
    return list(dedup.values())


def compact(value: Any, limit: int = 1200) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def title_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\b(full movie|latest nollywood movie|nollywood movie|african movie|official movie|official film|new movie|202[0-9])\b", " ", text)
    text = re.sub(r"\([^)]*(full movie|nollywood|african movie)[^)]*\)", " ", text)
    text = re.split(r"\s+[|•]\s+", text, maxsplit=1)[0]
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\b(the|a|an)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def year_value(value: Any) -> int:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else 0


def load_tmdb_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def tmdb_match(row: dict[str, Any], api_key: str, session: requests.Session) -> dict[str, Any] | None:
    media_type = str(row.get("type") or "").lower()
    if media_type not in {"film", "series", "documentary", "animation"}:
        return None
    endpoint = "tv" if media_type == "series" else "movie"
    query = title_key(row.get("title"))
    if len(query) < 3:
        return None
    params: dict[str, Any] = {
        "api_key": api_key,
        "language": "en-US",
        "query": query,
        "include_adult": "false",
        "page": 1,
    }
    wanted_year = year_value(row.get("year"))
    if wanted_year:
        params["first_air_date_year" if endpoint == "tv" else "year"] = wanted_year
    response = session.get(f"{TMDB_BASE}/search/{endpoint}", params=params, timeout=25)
    response.raise_for_status()
    candidates = response.json().get("results") or []
    wanted = title_key(row.get("title"))
    best: tuple[float, dict[str, Any]] | None = None
    for candidate in candidates[:12]:
        actual = title_key(
            candidate.get("title")
            or candidate.get("name")
            or candidate.get("original_title")
            or candidate.get("original_name")
        )
        if not actual:
            continue
        ratio = SequenceMatcher(None, wanted, actual).ratio()
        exact = wanted == actual
        candidate_year = year_value(candidate.get("release_date") or candidate.get("first_air_date"))
        year_ok = not wanted_year or not candidate_year or abs(wanted_year - candidate_year) <= 2
        art = bool(candidate.get("backdrop_path") or candidate.get("poster_path"))
        if not art or not year_ok:
            continue
        # Generic indie titles collide frequently. Require an exact normalized
        # title or a very strong fuzzy match.
        if not exact and ratio < 0.94:
            continue
        score = ratio + (0.12 if exact else 0.0) + min(float(candidate.get("popularity") or 0) / 1000, 0.04)
        if best is None or score > best[0]:
            best = (score, candidate)
    if not best:
        return None
    hit = best[1]
    return {
        "tmdbId": hit.get("id"),
        "tmdbKind": endpoint,
        "posterPath": hit.get("poster_path") or "",
        "backdropPath": hit.get("backdrop_path") or "",
        "tmdbTitle": hit.get("title") or hit.get("name") or "",
        "tmdbOverview": compact(hit.get("overview"), 1200),
        "tmdbReleaseDate": hit.get("release_date") or hit.get("first_air_date") or "",
    }


def hydrate_tmdb(records: list[dict[str, Any]], api_key: str, cache_path: Path, enabled: bool) -> int:
    if not enabled or not api_key:
        return 0
    cache = load_tmdb_cache(cache_path)
    session = requests.Session()
    changed = 0
    requested = 0
    for index, row in enumerate(records, start=1):
        if row.get("backdropPath") or row.get("posterPath"):
            continue
        if str(row.get("type") or "").lower() not in {"film", "series", "documentary", "animation"}:
            continue
        key = f"{row.get('type')}|{title_key(row.get('title'))}|{year_value(row.get('year'))}"
        result = cache.get(key, "__missing__")
        if result == "__missing__":
            try:
                result = tmdb_match(row, api_key, session)
            except Exception as exc:
                print(f"TMDB WARN {row.get('title')}: {exc}")
                result = None
            cache[key] = result
            requested += 1
            if requested % 25 == 0:
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
            time.sleep(0.035)
        if isinstance(result, dict):
            row.update(result)
            if not compact(row.get("description")) and result.get("tmdbOverview"):
                row["description"] = result["tmdbOverview"]
            changed += 1
        if index % 100 == 0:
            print(f"TMDB artwork {index:,}/{len(records):,} · matched {changed:,}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    return changed


def provider_label(provider: str) -> str:
    return {
        "youtube": "YouTube",
        "dailymotion": "Dailymotion",
        "vimeo": "Vimeo",
        "direct": "SUMMON",
    }.get(provider, provider.title())


def normalize_record(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    themes = [compact(item, 80) for item in (row.get("themes") or row.get("genres") or []) if compact(item, 80)]
    description = compact(row.get("description") or row.get("overview"), 1800)
    creator = compact(row.get("creator") or row.get("publisher"), 220)
    provider = str(row.get("provider") or "").lower()
    thumbnail = compact(row.get("thumbnail") or row.get("imageUrl"), 800)
    provider_url = compact(row.get("provider_url") or row.get("externalUrl"), 900)
    media_type = str(row.get("type") or "video").lower()

    result.update(
        {
            "sourceId": str(row.get("id") or f"{provider}:{row.get('provider_id', '')}"),
            "title": compact(row.get("title"), 360),
            "year": str(row.get("year") or ""),
            "type": media_type,
            "country": compact(row.get("country") or "Africa", 100),
            "languages": row.get("languages") or [],
            "runtime": compact(row.get("runtime"), 80),
            "publisher": creator,
            "creator": creator,
            "availability": f"play here · {provider_label(provider)}",
            "genres": themes[:12],
            "themes": themes,
            "overview": description,
            "description": description,
            "externalUrl": provider_url,
            "provider_url": provider_url,
            "imageUrl": thumbnail,
            "official": bool(row.get("official", True)),
            "embeddable": bool(row.get("embeddable", True)),
        }
    )
    result["candidate"] = candidate(result)
    return result


def candidate(row: dict[str, Any]) -> str:
    themes = ", ".join(map(str, row.get("themes") or row.get("genres") or []))
    languages = ", ".join(map(str, row.get("languages") or []))
    return (
        f"{row.get('title', '')}. Format: {row.get('type', 'video')}. "
        f"Country or cultural context: {row.get('country', 'Africa')}. "
        f"Languages: {languages}. Creator: {row.get('creator', '')}. "
        f"Themes and emotional ideas: {themes}. Description: {row.get('description', '')}"
    ).strip()


def extract_vectors(payload: Any) -> np.ndarray:
    if isinstance(payload, dict):
        for key in ("vectors", "embeddings", "data", "results"):
            if key not in payload:
                continue
            value = payload[key]
            if key == "data" and isinstance(value, list) and value and isinstance(value[0], dict):
                value = [item.get("embedding") or item.get("vector") for item in value]
            array = np.asarray(value, dtype=np.float32)
            if array.ndim == 1:
                array = array.reshape(1, -1)
            return array
        if "embedding" in payload:
            return np.asarray([payload["embedding"]], dtype=np.float32)
    if isinstance(payload, list):
        array = np.asarray(payload, dtype=np.float32)
        if array.ndim == 1:
            array = array.reshape(1, -1)
        return array
    raise ValueError("ARBITER response did not contain vectors")


def embed_batch(url: str, texts: list[str], timeout: int = 180) -> np.ndarray:
    last: Exception | None = None
    attempts = (
        {"texts": texts, "use_freq": True},
        {"texts": texts},
        {"input": texts},
        {"sentences": texts},
    )
    for body in attempts:
        try:
            response = requests.post(url, json=body, timeout=timeout)
            if response.ok:
                array = extract_vectors(response.json())
                if array.shape[0] == len(texts):
                    return array.astype(np.float32, copy=False)
            else:
                last = RuntimeError(f"HTTP {response.status_code}: {response.text[:160]}")
        except Exception as exc:
            last = exc
    raise RuntimeError(f"ARBITER embedding failed: {last or 'unrecognized response'}")


def hash_features(text: str) -> list[tuple[str, float]]:
    normalized = unicodedata.normalize("NFKD", text.lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens = re.findall(r"[a-z0-9']+", normalized)
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "for", "with", "that", "this", "is", "it",
        "format", "description", "themes", "creator", "country", "languages", "video", "film", "movie",
    }
    features: list[tuple[str, float]] = []
    for token in tokens:
        if len(token) > 2 and token not in stop:
            features.append((f"w:{token}", 1.0 + min(len(token), 12) / 24))
    collapsed = re.sub(r"\s+", " ", normalized).strip()
    for index in range(max(0, len(collapsed) - 3)):
        gram = collapsed[index : index + 4]
        if " " not in gram or gram.strip():
            features.append((f"c:{gram}", 0.16))
    return features


def hash_vector(text: str, dimension: int = DIMENSION) -> np.ndarray:
    vector = np.zeros(dimension, dtype=np.float32)
    for feature, weight in hash_features(text):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
        index = int.from_bytes(digest[:8], "little") % dimension
        sign = 1.0 if digest[8] & 1 else -1.0
        vector[index] += sign * float(weight)
    norm = float(np.linalg.norm(vector))
    if norm:
        vector /= norm
    return vector


def hash_matrix(texts: list[str], dimension: int = DIMENSION) -> np.ndarray:
    return np.vstack([hash_vector(text, dimension) for text in texts]).astype(np.float32)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_outputs(field_dir: Path, records: list[dict[str, Any]], matrix: np.ndarray, method: str, seconds: float) -> dict[str, Any]:
    field_dir.mkdir(parents=True, exist_ok=True)
    norms = np.linalg.norm(matrix, axis=1).astype(np.float32)
    norms[norms == 0] = 1
    matrix = (matrix / norms[:, None]).astype(np.float32, copy=False)
    ones = np.ones(len(records), dtype=np.float32)

    types: dict[str, int] = {}
    providers: dict[str, int] = {}
    for row in records:
        media_type = str(row.get("type") or "video")
        provider = str(row.get("provider") or "unknown")
        types[media_type] = types.get(media_type, 0) + 1
        providers[provider] = providers.get(provider, 0) + 1

    built_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest = {
        "name": "SUMMON AFRICA",
        "ready": True,
        "count": len(records),
        "playable": len(records),
        "dim": int(matrix.shape[1]),
        "dimension": int(matrix.shape[1]),
        "use_freq": True,
        "embedding_method": method,
        "types": types,
        "providers": providers,
        "sources": {name: {"count": count, "units": count} for name, count in providers.items()},
        "units": len(records),
        "built_at": built_at,
        "updated_at": built_at,
        "seconds": round(seconds, 3),
    }

    write_jsonl(field_dir / "metadata.jsonl", records)
    matrix.tofile(field_dir / "vectors.f32")
    ones.tofile(field_dir / "norms.f32")
    (field_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")

    # Browser bundle consumed directly by the restored SUMMON production frontend.
    browser_manifest = dict(manifest)
    browser_manifest.update(
        {
            "metadata_file": "SUMMON_field_metadata.json",
            "metadata_files": ["SUMMON_field_metadata.json"],
            "vectors_file": "SUMMON_field_vectors.f32",
            "norms_file": "SUMMON_field_norms.f32",
        }
    )
    (ROOT / "SUMMON_field_manifest.json").write_text(json.dumps(browser_manifest, indent=2), "utf-8")
    (ROOT / "SUMMON_field_metadata.json").write_text(json.dumps(records, ensure_ascii=False), "utf-8")
    matrix.tofile(ROOT / "SUMMON_field_vectors.f32")
    ones.tofile(ROOT / "SUMMON_field_norms.f32")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the SUMMON AFRICA playable 72D field")
    parser.add_argument("--embed-url", default=os.getenv("ARBITER_EMBED_URL", DEFAULT_ARBITER))
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--field-dir", default=str(ROOT / "field"))
    parser.add_argument("--tmdb-key", default=os.getenv("TMDB_API_KEY", DEFAULT_TMDB_KEY))
    parser.add_argument("--skip-tmdb", action="store_true")
    parser.add_argument("--force-hash", action="store_true")
    parser.add_argument("--min-records", type=int, default=int(os.getenv("SUMMON_AFRICA_MIN_RECORDS", "500")))
    args = parser.parse_args()

    started = time.time()
    imported = ROOT / "data/imported.jsonl"
    manual = ROOT / "data/manual.jsonl"
    records = load_records([ROOT / "data/seed.json", imported, manual])
    if len(records) < args.min_records:
        raise SystemExit(
            f"Refusing to build a production field with only {len(records):,} records; "
            f"minimum is {args.min_records:,}. Run ingestion first."
        )

    matched = hydrate_tmdb(
        records,
        str(args.tmdb_key or ""),
        ROOT / "data/tmdb_cache.json",
        not args.skip_tmdb,
    )
    normalized = [normalize_record(row) for row in records]
    texts = [row["candidate"] for row in normalized]

    method = "arbiter-72d"
    vectors: list[np.ndarray] = []
    if not args.force_hash:
        try:
            for index in range(0, len(texts), max(1, args.batch_size)):
                batch = texts[index : index + args.batch_size]
                print(f"ARBITER {index + 1:,}-{index + len(batch):,} of {len(texts):,}")
                vectors.append(embed_batch(args.embed_url, batch))
            matrix = np.vstack(vectors).astype(np.float32, copy=False)
            if matrix.shape[1] != DIMENSION:
                raise RuntimeError(f"expected {DIMENSION} dimensions, received {matrix.shape[1]}")
        except Exception as exc:
            print(f"ARBITER unavailable · deterministic local 72D fallback: {exc}")
            method = "local-hash-72d"
            matrix = hash_matrix(texts, DIMENSION)
    else:
        method = "local-hash-72d"
        matrix = hash_matrix(texts, DIMENSION)

    manifest = write_outputs(Path(args.field_dir), normalized, matrix, method, time.time() - started)
    print(f"TMDB artwork matched {matched:,}/{len(normalized):,}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
