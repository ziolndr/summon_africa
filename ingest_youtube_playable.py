#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

import requests

ROOT = Path(__file__).resolve().parent
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
NOISE = re.compile(
    r"\b(trailer|teaser|promo|preview|reaction|review|behind\s+the\s+scenes|"
    r"bts|soundtrack|lyrics?|official\s+audio|coming\s+soon)\b",
    re.I,
)


def compact(value: Any, limit: int = 1400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def iso_seconds(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def fmt_duration(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds:
        return f"{max(1, seconds // 60)}m"
    return ""


def classify(title: str, description: str, seconds: int, webpage_url: str = "") -> str:
    text = f"{title} {description}".lower()
    if "animation" in text or "animated" in text:
        return "animation"
    if "documentary" in text or "docuseries" in text or "africa eye" in text:
        return "documentary"
    if any(word in text for word in ("interview", "conversation", "podcast", "roundtable", "talks")):
        return "interview"
    if any(word in text for word in ("music video", "live performance", "acoustic", "session")):
        return "music"
    if "/shorts/" in webpage_url or "#shorts" in text or (0 < seconds <= 180):
        return "short"
    if any(word in text for word in ("episode", "web series", "season ", "series finale")):
        return "series"
    if 0 < seconds <= 22 * 60:
        return "short"
    return "film"


def likely_complete_work(title: str, description: str, media_type: str, seconds: int) -> bool:
    text = f"{title} {description}"
    if NOISE.search(text):
        return False
    if media_type == "film" and seconds and seconds < 22 * 60:
        return False
    if media_type == "documentary" and seconds and seconds < 4 * 60:
        return False
    return True


def best_thumbnail(info: dict[str, Any], video_id: str) -> str:
    thumbs = info.get("thumbnails") or []
    if isinstance(thumbs, dict):
        thumbs = list(thumbs.values())
    candidates = [item for item in thumbs if isinstance(item, dict) and item.get("url")]
    if candidates:
        candidates.sort(key=lambda item: (int(item.get("width") or 0) * int(item.get("height") or 0)))
        return str(candidates[-1]["url"])
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"


def normalize_channel(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def channel_score(query: str, item: dict[str, Any]) -> float:
    wanted = set(normalize_channel(query).split())
    actual = normalize_channel(
        str(item.get("channel") or item.get("uploader") or item.get("channel_id") or "")
    )
    tokens = set(actual.split())
    overlap = len(wanted & tokens) / max(1, len(wanted))
    exact = 1.0 if normalize_channel(query) == actual else 0.0
    return overlap + exact


def build_row(
    info: dict[str, Any],
    source: dict[str, Any],
    fallback_channel: str = "",
) -> dict[str, Any] | None:
    video_id = str(info.get("id") or "")
    if not VIDEO_ID.fullmatch(video_id):
        return None
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        return None
    availability = str(info.get("availability") or "public").lower()
    if availability in {"private", "premium_only", "subscriber_only", "needs_auth"}:
        return None

    title = compact(info.get("title"), 300)
    description = compact(info.get("description"), 1800)
    seconds = int(info.get("duration") or 0)
    webpage_url = str(info.get("webpage_url") or info.get("url") or "")
    media_type = classify(title, description, seconds, webpage_url)
    allowed = {str(item).lower() for item in source.get("types") or []}
    if allowed and media_type not in allowed:
        return None
    if not likely_complete_work(title, description, media_type, seconds):
        return None

    channel = compact(info.get("channel") or info.get("uploader") or fallback_channel or source["query"], 180)
    timestamp = info.get("timestamp") or info.get("release_timestamp") or 0
    upload_date = str(info.get("upload_date") or "")
    year = int(upload_date[:4]) if upload_date[:4].isdigit() else 0
    if not year and timestamp:
        try:
            year = time.gmtime(int(timestamp)).tm_year
        except Exception:
            year = 0

    tags = [compact(item, 80) for item in (info.get("tags") or []) if compact(item, 80)][:24]
    categories = [compact(item, 80) for item in (info.get("categories") or []) if compact(item, 80)][:8]
    themes = list(dict.fromkeys(tags + categories + [media_type, source.get("country", "Africa")]))[:30]
    language = compact(info.get("language") or "")
    return {
        "id": f"youtube:{video_id}",
        "title": title or video_id,
        "year": year,
        "type": media_type,
        "country": source.get("country", "Africa"),
        "languages": [language] if language else ["Unknown"],
        "runtime": fmt_duration(seconds),
        "runtime_seconds": seconds,
        "creator": channel,
        "provider": "youtube",
        "provider_id": video_id,
        "provider_url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": best_thumbnail(info, video_id),
        "official": True,
        "embeddable": True,
        "description": description or title,
        "themes": themes,
        "published_at": upload_date,
        "channel_id": compact(info.get("channel_id"), 80),
        "channel_url": compact(info.get("channel_url") or info.get("uploader_url"), 400),
    }


def yt_dlp_options(flat: bool = True, playlist_end: int | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "socket_timeout": 25,
        "retries": 2,
        "extractor_retries": 2,
        "noplaylist": False,
    }
    if flat:
        options["extract_flat"] = "in_playlist"
    if playlist_end:
        options["playlistend"] = playlist_end
    return options


def resolve_channel_with_ytdlp(query: str) -> tuple[str, str]:
    from yt_dlp import YoutubeDL

    with YoutubeDL(yt_dlp_options(flat=True, playlist_end=8)) as ydl:
        payload = ydl.extract_info(f"ytsearch8:{query}", download=False) or {}
    entries = [item for item in (payload.get("entries") or []) if isinstance(item, dict)]
    entries.sort(key=lambda item: channel_score(query, item), reverse=True)
    for item in entries:
        channel_url = str(item.get("channel_url") or item.get("uploader_url") or "").rstrip("/")
        if channel_url:
            return channel_url, str(item.get("channel") or item.get("uploader") or query)
    raise RuntimeError(f"could not resolve channel for {query}")


def enumerate_ytdlp_source(source: dict[str, Any], per_channel: int) -> list[dict[str, Any]]:
    from yt_dlp import YoutubeDL

    channel_url = str(source.get("url") or "").rstrip("/")
    channel_title = source["query"]
    if not channel_url:
        channel_url, channel_title = resolve_channel_with_ytdlp(source["query"])

    surface_limits = {
        "videos": max(10, int(per_channel * 0.7)),
        "shorts": max(10, int(per_channel * 0.5)),
    }
    flat_entries: dict[str, dict[str, Any]] = {}
    for surface, limit in surface_limits.items():
        url = f"{channel_url}/{surface}"
        try:
            with YoutubeDL(yt_dlp_options(flat=True, playlist_end=limit)) as ydl:
                payload = ydl.extract_info(url, download=False) or {}
            for item in payload.get("entries") or []:
                if isinstance(item, dict) and VIDEO_ID.fullmatch(str(item.get("id") or "")):
                    item.setdefault("channel", channel_title)
                    item.setdefault("channel_url", channel_url)
                    flat_entries[str(item["id"])] = item
        except Exception as exc:
            print(f"WARN {source['query']} {surface}: {exc}", file=sys.stderr)

    # Flat playlist records are enough for a fast large catalog. yt-dlp usually
    # includes title, duration, channel and thumbnails in these entries.
    rows: list[dict[str, Any]] = []
    for item in flat_entries.values():
        row = build_row(item, source, channel_title)
        if row:
            rows.append(row)
    return rows[:per_channel]


def youtube_api_get(path: str, key: str, **params: Any) -> dict[str, Any]:
    params["key"] = key
    response = requests.get(f"{YOUTUBE_API}/{path}", params=params, timeout=45)
    response.raise_for_status()
    return response.json()


def resolve_channel_api(key: str, query: str) -> tuple[str, str]:
    payload = youtube_api_get("search", key, part="snippet", type="channel", q=query, maxResults=5)
    items = payload.get("items") or []
    if not items:
        raise RuntimeError(f"no channel found for {query}")
    item = max(items, key=lambda value: channel_score(query, value.get("snippet") or {}))
    return item["snippet"]["channelId"], item["snippet"]["channelTitle"]


def uploads_playlist(key: str, channel_id: str) -> str:
    payload = youtube_api_get("channels", key, part="contentDetails", id=channel_id)
    return payload["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]


def playlist_ids(key: str, playlist_id: str, limit: int) -> list[str]:
    output: list[str] = []
    token = None
    while len(output) < limit:
        params: dict[str, Any] = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": min(50, limit - len(output)),
        }
        if token:
            params["pageToken"] = token
        payload = youtube_api_get("playlistItems", key, **params)
        output.extend(item["contentDetails"]["videoId"] for item in payload.get("items") or [])
        token = payload.get("nextPageToken")
        if not token:
            break
    return output[:limit]


def enumerate_api_source(source: dict[str, Any], api_key: str, per_channel: int) -> list[dict[str, Any]]:
    channel_id, channel_title = resolve_channel_api(api_key, source["query"])
    ids = playlist_ids(api_key, uploads_playlist(api_key, channel_id), per_channel)
    rows: list[dict[str, Any]] = []
    for index in range(0, len(ids), 50):
        payload = youtube_api_get(
            "videos",
            api_key,
            part="snippet,contentDetails,status",
            id=",".join(ids[index : index + 50]),
            maxResults=50,
        )
        for item in payload.get("items") or []:
            status = item.get("status") or {}
            if status.get("privacyStatus") != "public" or status.get("embeddable") is False:
                continue
            snippet = item.get("snippet") or {}
            details = item.get("contentDetails") or {}
            seconds = iso_seconds(details.get("duration", ""))
            info = {
                "id": item["id"],
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "duration": seconds,
                "channel": channel_title,
                "channel_id": channel_id,
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "tags": snippet.get("tags") or [],
                "language": snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage"),
                "upload_date": str(snippet.get("publishedAt") or "").replace("-", "")[:8],
                "thumbnails": list((snippet.get("thumbnails") or {}).values()),
                "availability": "public",
            }
            row = build_row(info, source, channel_title)
            if row:
                rows.append(row)
    return rows


def load_sources(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("sources config must be a JSON array")
    return [item for item in payload if isinstance(item, dict) and item.get("query")]


def write_rows(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a large creator-owned, in-app-playable African YouTube catalog")
    parser.add_argument("--api-key", default=os.getenv("YOUTUBE_API_KEY"))
    parser.add_argument("--sources", default=str(ROOT / "config/sources.json"))
    parser.add_argument("--per-channel", type=int, default=45)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--max-records", type=int, default=1800)
    parser.add_argument("--output", default=str(ROOT / "data/imported.jsonl"))
    parser.add_argument("--force-ytdlp", action="store_true")
    args = parser.parse_args()

    sources = load_sources(Path(args.sources))
    use_api = bool(args.api_key) and not args.force_ytdlp
    mode = "YouTube Data API" if use_api else "yt-dlp public channel discovery"
    print(f"SUMMON AFRICA playable ingest · {len(sources)} sources · {mode}")

    rows: dict[str, dict[str, Any]] = {}

    def run(source: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        if use_api:
            result = enumerate_api_source(source, str(args.api_key), args.per_channel)
        else:
            result = enumerate_ytdlp_source(source, args.per_channel)
        return source["query"], result

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        jobs = {executor.submit(run, source): source for source in sources}
        for future in as_completed(jobs):
            source = jobs[future]
            try:
                name, found = future.result()
                for row in found:
                    rows[row["id"]] = row
                print(f"{name}: {len(found):,} playable works · total {len(rows):,}")
            except Exception as exc:
                print(f"WARN {source['query']}: {exc}", file=sys.stderr)
            if len(rows) >= args.max_records:
                break

    ordered = sorted(rows.values(), key=lambda row: (str(row.get("published_at") or ""), row["title"]), reverse=True)
    ordered = ordered[: args.max_records]
    write_rows(Path(args.output), ordered)
    print(f"wrote {len(ordered):,} playable records → {args.output}")
    if len(ordered) < 100:
        print("WARN catalog is below 100 records; rerun with a YouTube API key or more source channels.", file=sys.stderr)
    return 0 if ordered else 2


if __name__ == "__main__":
    raise SystemExit(main())
