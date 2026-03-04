# Podcast Clip Hub

An AI-powered tool that ingests podcast RSS feeds, transcribes episodes with local Whisper, identifies the most compelling moments using Claude, extracts shareable audio clips, and presents them in a filterable static web app.

Originally built for sports media podcasts — but fully configurable for any topic area by editing `podcasts.json`.

## How it works

```
RSS feeds → download audio → Whisper transcription →
Claude analysis → FFmpeg clip extraction → clips.json → static web app
```

The pipeline runs locally and outputs a static site you can serve anywhere (GitHub Pages, Netlify, etc.). No server required after the pipeline runs.

## Setup

### 1. Install dependencies

**Create a virtual environment** (recommended — avoids system Python conflicts):
```bash
cd podcast-clip-hub
python3 -m venv .venv
source .venv/bin/activate
```

**Python packages:**
```bash
pip install openai-whisper anthropic feedparser
```

**FFmpeg** (for audio clipping):
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

**Whisper model** will be downloaded automatically on first run (~140 MB for `base`, ~75 MB for `tiny`).

> **Note:** Activate the venv (`source .venv/bin/activate`) each time you open a new terminal session before running the pipeline.

### 2. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Configure your podcasts

Edit `podcasts.json`:

- **`podcasts`** — list of RSS feed URLs. Set `"enabled": false` to skip a feed.
- **`key_people`** — names Claude should always flag when mentioned substantively.
- **`topics`** — topic keywords to prioritize.
- **`max_episodes_per_feed`** — how many recent episodes to check per feed (default: 5).
- **`clip_padding_seconds`** — seconds of audio padding before/after each clip (default: 20).
- **`whisper_model`** — `tiny` (fastest), `base` (default), `small`, `medium`, `large`.
- **`min_relevance_score`** — Claude scores clips 1–10; only clips ≥ this score are kept (default: 7).
- **`anthropic_model`** — Claude model to use (default: `claude-opus-4-6`).

### 4. Run the pipeline

```bash
# From the project root directory:
python3 scripts/process_podcasts.py
```

Options:
```
--config PATH         Config file (default: podcasts.json)
--output PATH         Output JSON (default: clips.json)
--cache-dir PATH      Cache directory (default: cache/)
--max-episodes N      Override max_episodes_per_feed
--feed NAME           Process only feeds whose name contains NAME
--whisper-model MODEL Override whisper_model in config
--dry-run             Analyze but don't extract clips or write output
```

**Examples:**
```bash
# Test on one feed with the smallest Whisper model
python3 scripts/process_podcasts.py --feed "Press Box" --max-episodes 1 --whisper-model tiny

# Dry run to see what Claude would flag, without extracting clips
python3 scripts/process_podcasts.py --dry-run --max-episodes 2

# Full run
python3 scripts/process_podcasts.py
```

### 5. Preview locally

```bash
python3 -m http.server 8001
# Open http://localhost:8001
```

## Performance notes

- **Whisper transcription** takes roughly 5–30 min per episode depending on hardware and model size.
- Transcripts and Claude analysis are **cached** in `cache/`. Re-runs are fast — only new episodes are processed.
- Raw episode audio is cached in `cache/audio/` (not committed to git). Only the clipped segments (`clips/`) are kept long-term.

## File structure

```
podcast-clip-hub/
├── podcasts.json          # ← Edit this to configure your feeds + topics
├── clips.json             # Pipeline output (committed, consumed by web app)
├── index.html             # Web app
├── app.js                 # Frontend logic
├── styles.css             # Styles
├── clips/                 # Audio clip files (committed if small enough)
│   └── {clip_id}.mp3
├── cache/                 # Gitignored: raw audio + transcripts
│   ├── audio/
│   ├── transcripts/
│   └── analysis/
└── scripts/
    └── process_podcasts.py
```

## Publishing to GitHub Pages

1. Create a new GitHub repo.
2. Push this project:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git add .
   git commit -m "Initial podcast clip hub"
   git push -u origin main
   ```
3. In GitHub: **Settings → Pages → Source → GitHub Actions**.
4. The `deploy-pages.yml` workflow will deploy automatically on every push to `main`.

## Sharing clips

Each clip card has a **Copy link** button that copies a URL with `?clip=<id>`. When someone opens that URL, the app scrolls to and highlights the specific clip.

## Adapting for a different topic

1. Replace the `podcasts` list in `podcasts.json` with your feeds.
2. Update `key_people` and `topics` to match your domain.
3. Optionally update `min_relevance_score` and `max_episodes_per_feed`.
4. Run the pipeline.

The pipeline and web app are fully topic-agnostic — Claude's analysis adapts to whatever context you provide in the config.
