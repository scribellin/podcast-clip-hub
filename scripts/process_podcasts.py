#!/usr/bin/env python3
"""
process_podcasts.py — Podcast Clip Hub pipeline

Fetches RSS feeds → downloads audio → transcribes with local Whisper →
analyzes with Claude to find compelling segments → extracts clips via FFmpeg →
writes clips.json for the static web app.

Usage:
  python3 scripts/process_podcasts.py [options]

Requirements:
  pip install openai-whisper anthropic feedparser
  brew install ffmpeg  (macOS) | apt install ffmpeg  (Linux)

Environment:
  ANTHROPIC_API_KEY  — required for segment analysis
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ── Dependency checks ──────────────────────────────────────────────────────────

try:
    import feedparser
except ImportError:
    sys.exit("Missing dependency: pip install feedparser")

try:
    import whisper as _whisper_module
except ImportError:
    sys.exit("Missing dependency: pip install openai-whisper")

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency: pip install anthropic")


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ── RSS fetching ───────────────────────────────────────────────────────────────

def fetch_feed(podcast: dict, max_episodes: int) -> tuple:
    """Fetch RSS feed. Returns (episodes, podcast_meta)."""
    print(f"  Fetching feed...")
    feed = feedparser.parse(podcast["rss"])
    if feed.bozo and not feed.entries:
        print(f"  ⚠  Failed to parse feed for '{podcast['name']}': {feed.bozo_exception}")
        return [], {}

    # Extract podcast-level metadata
    fi = feed.feed
    artwork_url = (
        fi.get("itunes_image", {}).get("href")
        or fi.get("image", {}).get("href")
        or ""
    )
    description = (
        fi.get("subtitle")
        or fi.get("summary")
        or fi.get("description")
        or ""
    )
    # Strip HTML from description
    description = re.sub(r"<[^>]+>", "", description).strip()[:300]

    podcast_meta = {
        "name": podcast["name"],
        "rss": podcast["rss"],
        "artwork_url": artwork_url,
        "description": description,
    }

    episodes = []
    for entry in feed.entries[:max_episodes]:
        # Get audio URL from enclosures
        audio_url = None
        for enc in entry.get("enclosures", []):
            t = enc.get("type", "")
            if t.startswith("audio") or t == "application/octet-stream":
                audio_url = enc.get("href") or enc.get("url")
                break

        if not audio_url:
            continue

        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        episode_id = hashlib.sha1(guid.encode()).hexdigest()[:16]

        # Parse date
        date_str = ""
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d")
            except Exception:
                pass

        # Episode-level artwork (falls back to show artwork)
        ep_artwork = (
            entry.get("itunes_image", {}).get("href")
            or entry.get("image", {}).get("href")
            or artwork_url
        )

        # Podcast:transcript URLs (Podcasting 2.0 namespace)
        transcript_urls = _get_transcript_urls(entry)

        episodes.append(
            {
                "id": episode_id,
                "podcast_name": podcast["name"],
                "title": entry.get("title", "Untitled"),
                "date": date_str,
                "url": entry.get("link", ""),
                "audio_url": audio_url,
                "episode_artwork": ep_artwork,
                "transcript_urls": transcript_urls,
            }
        )

    return episodes, podcast_meta


# ── Audio downloading ──────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def download_audio(audio_url: str, output_path: Path) -> bool:
    """Stream-download podcast audio. Returns True on success."""
    if output_path.exists() and output_path.stat().st_size > 100_000:
        print(f"    Audio cached.")
        return True

    print(f"    Downloading audio...", flush=True)
    req = urllib.request.Request(audio_url, headers=_HEADERS)

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=180) as response:
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            with open(output_path, "wb") as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        mb_done = downloaded / 1_000_000
                        mb_total = total / 1_000_000
                        print(
                            f"\r    {pct:5.1f}%  {mb_done:.0f} / {mb_total:.0f} MB",
                            end="",
                            flush=True,
                        )
        print()
        return True
    except Exception as e:
        print(f"\n    ✗ Download failed: {e}")
        if output_path.exists():
            output_path.unlink()
        return False


# ── RSS transcript helpers ─────────────────────────────────────────────────────

def _get_transcript_urls(entry) -> list:
    """Extract podcast:transcript URLs from a feedparser entry.
    Returns list of (url, mime_type) sorted by preference (JSON > VTT > SRT > plain)."""
    candidates = []
    # feedparser 6.x exposes podcast:transcript as entry.podcast_transcript
    for t in entry.get("podcast_transcript", []):
        url = t.get("url", "")
        mime = t.get("type", "application/json").split(";")[0].strip()
        if url:
            candidates.append((url, mime))
    priority = {
        "application/json": 0,
        "text/vtt": 1,
        "text/srt": 2,
        "application/x-subrip": 2,
        "text/plain": 3,
        "text/html": 4,
    }
    candidates.sort(key=lambda x: priority.get(x[1], 99))
    return candidates


def _ts_to_seconds(ts: str) -> float:
    """Convert VTT/SRT timestamp (HH:MM:SS.mmm or MM:SS.mmm) to seconds."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _parse_transcript_json(content: str):
    """Parse Podcast Index JSON transcript format."""
    data = json.loads(content)
    segments = []
    for seg in data.get("segments", []):
        text = (seg.get("body") or seg.get("text") or "").strip()
        if not text:
            continue
        segments.append({
            "start": float(seg.get("startTime", 0)),
            "end":   float(seg.get("endTime", seg.get("startTime", 0) + 5)),
            "text":  " " + text,
            "words": [],
        })
    return {"text": " ".join(s["text"] for s in segments), "segments": segments} if segments else None


def _parse_transcript_vtt(content: str):
    """Parse WebVTT transcript."""
    segments = []
    for block in re.split(r"\n\n+", content):
        lines = block.strip().splitlines()
        ts_line = None
        text_parts = []
        for line in lines:
            m = re.match(
                r"(\d{1,2}:\d{2}[:.]\d{2}[.,]\d+)\s*-->\s*(\d{1,2}:\d{2}[:.]\d{2}[.,]\d+)",
                line,
            )
            if m:
                ts_line = (_ts_to_seconds(m.group(1)), _ts_to_seconds(m.group(2)))
            elif ts_line and line.strip() and not re.match(r"^\d+$", line.strip()):
                clean = re.sub(r"<[^>]+>", "", line).strip()
                if clean:
                    text_parts.append(clean)
        if ts_line and text_parts:
            segments.append({
                "start": ts_line[0], "end": ts_line[1],
                "text": " " + " ".join(text_parts), "words": [],
            })
    return {"text": " ".join(s["text"] for s in segments), "segments": segments} if segments else None


def _parse_transcript_srt(content: str):
    """Parse SRT subtitle format."""
    segments = []
    for block in re.split(r"\n\n+", content):
        lines = block.strip().splitlines()
        if not lines:
            continue
        start_idx = 1 if lines[0].strip().isdigit() else 0
        if start_idx >= len(lines):
            continue
        m = re.match(
            r"(\d{2}:\d{2}:\d{2}[,.]?\d*)\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]?\d*)",
            lines[start_idx],
        )
        if not m:
            continue
        text = " ".join(lines[start_idx + 1:]).strip()
        if text:
            segments.append({
                "start": _ts_to_seconds(m.group(1)), "end": _ts_to_seconds(m.group(2)),
                "text": " " + text, "words": [],
            })
    return {"text": " ".join(s["text"] for s in segments), "segments": segments} if segments else None


def _parse_transcript_plain(content: str):
    """Parse plain-text transcript (no real timestamps — splits into chunks)."""
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    if len(content) < 100:
        return None
    words = content.split()
    chunk = 200  # words per fake segment
    segments = []
    for i in range(0, len(words), chunk):
        text = " ".join(words[i : i + chunk])
        segments.append({
            "start": i * 0.4, "end": (i + chunk) * 0.4,
            "text": " " + text, "words": [],
        })
    return {"text": content, "segments": segments, "_no_timestamps": True}


def try_rss_transcript(ep: dict, transcript_path: Path):
    """Try to download a published transcript. Returns transcript dict or None."""
    if transcript_path.exists():
        with open(transcript_path) as f:
            cached = json.load(f)
        if cached.get("_source") == "rss_transcript":
            print(f"    RSS transcript cached.")
            return cached
        # Whisper cache exists — use it, don't re-download
        return None

    urls = ep.get("transcript_urls", [])
    if not urls:
        return None

    for url, mime in urls:
        print(f"    Fetching RSS transcript ({mime.split('/')[-1]})…", flush=True)
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                content = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"    ⚠  Transcript fetch failed: {e}")
            continue

        transcript = None
        if "json" in mime:
            transcript = _parse_transcript_json(content)
        elif "vtt" in mime:
            transcript = _parse_transcript_vtt(content)
        elif "srt" in mime or "subrip" in mime:
            transcript = _parse_transcript_srt(content)
        elif "plain" in mime or "html" in mime:
            transcript = _parse_transcript_plain(content)

        if transcript and transcript.get("segments"):
            transcript["_source"] = "rss_transcript"
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            with open(transcript_path, "w") as f:
                json.dump(transcript, f)
            n = len(transcript["segments"])
            flag = " (no timestamps — clip accuracy limited)" if transcript.get("_no_timestamps") else ""
            print(f"    ✓ RSS transcript: {n} segments{flag}")
            return transcript

    return None


# ── Whisper transcription ──────────────────────────────────────────────────────

_whisper_model_cache = {}


def get_whisper_model(model_name: str):
    if model_name not in _whisper_model_cache:
        print(f"  Loading Whisper model '{model_name}' (first time may download ~{_model_size(model_name)})...")
        _whisper_model_cache[model_name] = _whisper_module.load_model(model_name)
    return _whisper_model_cache[model_name]


def _model_size(name: str) -> str:
    sizes = {"tiny": "75 MB", "base": "140 MB", "small": "460 MB", "medium": "1.4 GB", "large": "2.9 GB"}
    return sizes.get(name, "unknown size")


def transcribe_audio(audio_path: Path, transcript_path: Path, model_name: str):
    """Transcribe audio file. Returns transcript dict or None on failure."""
    if transcript_path.exists():
        print(f"    Transcript cached.")
        with open(transcript_path) as f:
            return json.load(f)

    print(f"    Transcribing with Whisper '{model_name}' (may take several minutes)...", flush=True)
    model = get_whisper_model(model_name)

    try:
        result = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            verbose=False,
        )

        transcript = {
            "text": result["text"],
            "segments": [
                {
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"],
                    "words": [
                        {"word": w["word"], "start": w["start"], "end": w["end"]}
                        for w in seg.get("words", [])
                    ],
                }
                for seg in result["segments"]
            ],
        }

        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with open(transcript_path, "w") as f:
            json.dump(transcript, f)

        print(f"    Transcribed {len(transcript['segments'])} segments.")
        return transcript

    except Exception as e:
        print(f"    ✗ Transcription failed: {e}")
        return None


# ── Claude analysis ────────────────────────────────────────────────────────────

def _seconds_to_ts(s: float) -> str:
    s = int(s)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_transcript(transcript: dict) -> str:
    """Format transcript segments with HH:MM:SS timestamps for Claude."""
    lines = []
    for seg in transcript["segments"]:
        ts = _seconds_to_ts(seg["start"])
        lines.append(f"[{ts}] {seg['text'].strip()}")
    return "\n".join(lines)


def _parse_claude_json(raw: str) -> list:
    """Parse JSON from Claude response, stripping any markdown fences."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw)


def analyze_transcript(
    transcript: dict,
    episode: dict,
    config: dict,
    analysis_path: Path,
    client,
) -> list:
    """Use Claude to identify compelling segments. Returns list of segment dicts."""
    if analysis_path.exists():
        with open(analysis_path) as f:
            cached = json.load(f)
        if cached:  # Only skip if cache has real results (non-empty)
            print(f"    Analysis cached.")
            return cached
        # Empty cache likely means a prior API error — delete and retry
        analysis_path.unlink()

    print(f"    Analyzing with Claude...", flush=True)

    key_people = ", ".join(config.get("key_people", []))
    topics = ", ".join(config.get("topics", []))
    model = config.get("anthropic_model", "claude-opus-4-6")

    transcript_text = format_transcript(transcript)

    # Split into chunks if very long (>180K chars; most episodes fit in one)
    MAX_CHARS = 180_000
    if len(transcript_text) <= MAX_CHARS:
        chunks = [transcript_text]
    else:
        lines = transcript_text.split("\n")
        chunks, current, current_len = [], [], 0
        for line in lines:
            if current_len + len(line) > MAX_CHARS and current:
                chunks.append("\n".join(current))
                # 10% overlap to catch segment boundaries
                overlap = max(0, len(current) - len(current) // 10)
                current = current[overlap:]
                current_len = sum(len(l) for l in current)
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("\n".join(current))

    system_prompt = f"""You analyze podcast transcripts to surface the most compelling moments.

PODCAST: {episode["podcast_name"]}
EPISODE: {episode["title"]}

KEY PEOPLE — flag when mentioned substantively (not just in passing):
{key_people}

KEY TOPICS — flag when discussed with specificity:
{topics}

Find 3–8 segments containing genuinely informative, surprising, or analytically valuable content. Prioritize:
- Specific announcements, deals, or strategic moves by networks/streamers
- Candid insider analysis about industry players
- Notable takes on trends (streaming wars, media rights, ratings)
- Executives or decision-makers discussed in depth
- Revenue figures, viewership data, or business metrics
- Industry controversy or informed debate

Skip: intros/outros, sponsor reads, off-topic banter, vague surface commentary.

Timestamps in the transcript are formatted as [HH:MM:SS]. Use seconds for start_time and end_time.

Return ONLY a valid JSON array — no markdown, no commentary outside the JSON. Each element:
{{
  "start_time": <number: seconds>,
  "end_time": <number: seconds>,
  "quote": "<verbatim 1–3 sentence excerpt — the single most compelling line>",
  "reason": "<1–2 sentences explaining why this moment matters>",
  "topics": ["<matching topic>"],
  "people_mentioned": ["<name>"],
  "relevance_score": <integer 1–10>
}}"""

    all_segments = []
    had_successful_call = False

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"    Chunk {i + 1}/{len(chunks)}...", flush=True)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": f"TRANSCRIPT:\n\n{chunk}"}],
            )
            had_successful_call = True
            segments = _parse_claude_json(response.content[0].text)
            if isinstance(segments, list):
                all_segments.extend(segments)
        except json.JSONDecodeError as e:
            print(f"    ⚠  Claude returned invalid JSON: {e}")
            had_successful_call = True  # API responded, just bad JSON
        except Exception as e:
            print(f"    ✗ Claude analysis error: {e}")

    # Deduplicate: if two segments start within 30s, keep the higher-scored one
    all_segments.sort(key=lambda s: s.get("start_time", 0))
    deduped = []
    for seg in all_segments:
        if deduped and abs(seg.get("start_time", 0) - deduped[-1].get("start_time", 0)) < 30:
            if seg.get("relevance_score", 0) > deduped[-1].get("relevance_score", 0):
                deduped[-1] = seg
        else:
            deduped.append(seg)

    # Filter by minimum relevance score
    min_score = config.get("min_relevance_score", 7)
    deduped = [s for s in deduped if s.get("relevance_score", 0) >= min_score]

    # Only write cache if we actually got a response from Claude.
    # If all API calls failed (e.g. bad key), don't cache [] — let the next run retry.
    if had_successful_call:
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        with open(analysis_path, "w") as f:
            json.dump(deduped, f, indent=2)

    return deduped


# ── FFmpeg ─────────────────────────────────────────────────────────────────────

def check_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def extract_clip(
    audio_path: Path,
    start_time: float,
    end_time: float,
    output_path: Path,
    padding: int = 20,
) -> bool:
    """Extract audio segment with padding. Returns True on success."""
    start = max(0.0, start_time - padding)
    end = end_time + padding

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(audio_path),
        "-ss", str(start),
        "-to", str(end),
        "-c", "copy",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"    ✗ ffmpeg error: {result.stderr.decode()[:200]}")
        return False
    return True


# ── Clip ID ────────────────────────────────────────────────────────────────────

def make_clip_id(episode_id: str, start_time: float) -> str:
    return hashlib.sha1(f"{episode_id}:{start_time}".encode()).hexdigest()[:12]


# ── Metadata-only fetch ────────────────────────────────────────────────────────

def fetch_meta_only(args):
    """Fetch RSS metadata for all feeds and write podcasts_meta.json. No audio processing."""
    config_path = Path(args.config)
    output_path = Path(args.output)
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")
    config = load_config(config_path)

    meta_path = output_path.parent / "podcasts_meta.json"
    existing = {}
    if meta_path.exists():
        with open(meta_path) as f:
            try:
                existing = {m["name"]: m for m in json.load(f)}
            except json.JSONDecodeError:
                pass

    podcasts = [p for p in config.get("podcasts", []) if p.get("enabled", True)]
    results = []
    for podcast in podcasts:
        bar = "─" * 50
        print(f"{bar}\n  {podcast['name']}")
        _, meta = fetch_feed(podcast, max_episodes=0)
        merged = existing.get(podcast["name"], {})
        merged.update({k: v for k, v in meta.items() if v})  # only overwrite non-empty
        results.append({
            "name": podcast["name"],
            "rss": podcast.get("rss", ""),
            "artwork_url": merged.get("artwork_url", ""),
            "description": merged.get("description", ""),
        })
        if merged.get("artwork_url"):
            print(f"  ✓ artwork found")
        else:
            print(f"  – no artwork in feed")

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Wrote {len(results)} podcasts → {meta_path}")


# ── Main pipeline ──────────────────────────────────────────────────────────────

def run_pipeline(args):
    config_path = Path(args.config)
    output_path = Path(args.output)
    cache_dir = Path(args.cache_dir)

    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")

    config = load_config(config_path)
    max_episodes = args.max_episodes or config.get("max_episodes_per_feed", 5)
    whisper_model = args.whisper_model or config.get("whisper_model", "base")
    padding = config.get("clip_padding_seconds", 20)

    if not check_ffmpeg():
        sys.exit(
            "FFmpeg not found.\n"
            "  macOS: brew install ffmpeg\n"
            "  Linux: sudo apt install ffmpeg"
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit(
            "ANTHROPIC_API_KEY environment variable not set.\n"
            "  export ANTHROPIC_API_KEY=sk-ant-..."
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Load existing clips
    clips = []
    if output_path.exists():
        with open(output_path) as f:
            try:
                clips = json.load(f)
            except json.JSONDecodeError:
                clips = []
    existing_clip_ids = {c["id"] for c in clips}

    podcasts = config.get("podcasts", [])
    if args.feed:
        podcasts = [p for p in podcasts if args.feed.lower() in p["name"].lower()]
        if not podcasts:
            sys.exit(f"No podcast matching '{args.feed}'")

    # Load existing podcast metadata
    meta_path = output_path.parent / "podcasts_meta.json"
    all_podcast_meta = {}
    if meta_path.exists():
        with open(meta_path) as f:
            try:
                existing_meta = json.load(f)
                all_podcast_meta = {m["name"]: m for m in existing_meta}
            except json.JSONDecodeError:
                pass

    new_clips = []
    total_episodes = 0
    skipped_episodes = 0

    for podcast in podcasts:
        if not podcast.get("enabled", True):
            continue

        bar = "═" * 58
        print(f"\n{bar}")
        print(f"  {podcast['name']}")
        print(f"{bar}")

        episodes, podcast_meta = fetch_feed(podcast, max_episodes)
        if podcast_meta:
            all_podcast_meta[podcast["name"]] = podcast_meta
        print(f"  Found {len(episodes)} episodes in feed")

        for ep in episodes:
            total_episodes += 1
            print(f"\n  ▶ {ep['title'][:70]}")
            print(f"    {ep['date']}")

            audio_path = cache_dir / "audio" / f"{ep['id']}.mp3"
            transcript_path = cache_dir / "transcripts" / f"{ep['id']}.json"
            analysis_path = cache_dir / "analysis" / f"{ep['id']}.json"

            # Check if we already have all clips for this episode's analysis
            if analysis_path.exists():
                with open(analysis_path) as f:
                    existing_segs = json.load(f)
                all_present = all(
                    make_clip_id(ep["id"], s.get("start_time", 0)) in existing_clip_ids
                    for s in existing_segs
                    if s.get("relevance_score", 0) >= config.get("min_relevance_score", 7)
                )
                if all_present and existing_segs:
                    print(f"    All clips already processed, skipping.")
                    skipped_episodes += 1
                    continue

            # 1. Try RSS transcript first (fast — no audio download needed)
            transcript = try_rss_transcript(ep, transcript_path)

            # 2. Fall back to Whisper if no RSS transcript available
            if transcript is None:
                if not transcript_path.exists():
                    ok = download_audio(ep["audio_url"], audio_path)
                    if not ok:
                        continue
                transcript = transcribe_audio(audio_path, transcript_path, whisper_model)
                if transcript is None:
                    continue

            # 3. Analyze with Claude
            segments = analyze_transcript(transcript, ep, config, analysis_path, client)
            print(f"    Found {len(segments)} relevant segments (score ≥ {config.get('min_relevance_score', 7)})")

            if args.dry_run:
                for seg in segments:
                    ts = _seconds_to_ts(seg.get("start_time", 0))
                    score = seg.get("relevance_score", "?")
                    print(f"      [{score}/10] @{ts} — {seg.get('reason', '')[:80]}")
                continue

            # 4. Extract audio clips
            for seg in segments:
                cid = make_clip_id(ep["id"], seg["start_time"])

                if cid in existing_clip_ids:
                    continue

                clip_rel = Path("clips") / f"{cid}.mp3"
                clip_abs = output_path.parent / clip_rel

                print(f"    Extracting clip {cid}...", flush=True)
                ok = extract_clip(audio_path, seg["start_time"], seg["end_time"], clip_abs, padding)
                if not ok:
                    continue

                new_clips.append(
                    {
                        "id": cid,
                        "podcast_name": ep["podcast_name"],
                        "episode_title": ep["title"],
                        "episode_date": ep["date"],
                        "episode_url": ep["url"],
                        "episode_artwork": ep.get("episode_artwork", ""),
                        "transcript": seg.get("quote", ""),
                        "reason": seg.get("reason", ""),
                        "topics": seg.get("topics", []),
                        "people": seg.get("people_mentioned", []),
                        "relevance_score": seg.get("relevance_score", 0),
                        "clip_audio": str(clip_rel),
                        "start_time": seg["start_time"],
                        "end_time": seg["end_time"],
                    }
                )
                existing_clip_ids.add(cid)

    if args.dry_run:
        print(f"\n(dry run — {total_episodes} episodes scanned, no output written)")
        return

    # Merge, sort by date descending, write clips
    all_clips = clips + new_clips
    all_clips.sort(key=lambda c: c.get("episode_date", ""), reverse=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(all_clips, f, indent=2, ensure_ascii=False)

    # Write podcast metadata (merge with full config list so all feeds appear in sidebar)
    config_pods = {p["name"]: p for p in config.get("podcasts", []) if p.get("enabled", True)}
    merged_meta = []
    for name, pod in config_pods.items():
        meta = all_podcast_meta.get(name, {})
        merged_meta.append({
            "name": name,
            "rss": pod.get("rss", ""),
            "artwork_url": meta.get("artwork_url", ""),
            "description": meta.get("description", ""),
        })
    with open(meta_path, "w") as f:
        json.dump(merged_meta, f, indent=2, ensure_ascii=False)

    print(
        f"\n✓  Done.  {len(new_clips)} new clips added."
        f"  {skipped_episodes}/{total_episodes} episodes skipped (already processed)."
        f"  Total: {len(all_clips)} clips → {output_path}"
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Podcast Clip Hub — pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="podcasts.json", help="Path to config file (default: podcasts.json)")
    parser.add_argument("--output", default="clips.json", help="Output JSON file (default: clips.json)")
    parser.add_argument("--cache-dir", default="cache", help="Cache directory (default: cache/)")
    parser.add_argument("--max-episodes", type=int, metavar="N", help="Override max_episodes_per_feed from config")
    parser.add_argument("--feed", metavar="NAME", help="Process only feeds whose name contains NAME")
    parser.add_argument("--dry-run", action="store_true", help="Transcribe + analyze but skip clip extraction and JSON write")
    parser.add_argument("--fetch-meta", action="store_true", help="Fetch RSS metadata + artwork only, write podcasts_meta.json, then exit (no audio processing)")
    parser.add_argument(
        "--whisper-model",
        choices=["tiny", "base", "small", "medium", "large"],
        metavar="MODEL",
        help="Whisper model size (tiny/base/small/medium/large). Overrides config.",
    )
    args = parser.parse_args()

    if args.fetch_meta:
        fetch_meta_only(args)
    else:
        run_pipeline(args)


if __name__ == "__main__":
    main()
