# Playlist Lyric Video Pipeline

Desktop tool for turning a YouTube or YouTube Music playlist into a batch of lyric videos.

It builds a render queue from:

- playlist and album URLs from YouTube / YouTube Music
- track metadata extracted with `yt-dlp`
- lyrics resolved from Genie first, then lrclib
- OpenAI-based Korean-to-English lyric translation
- MP4 output or Premiere XML markers

## Current workflow

1. Paste a playlist URL such as `https://music.youtube.com/playlist?list=...` or `https://www.youtube.com/playlist?list=...`.
2. Choose a lyrics policy:
   - allow plain lyrics if synced lyrics are unavailable
   - queue only tracks with synced lyrics
3. Analyze the playlist.
4. Review the generated render queue and the skipped-track list.
5. Run the batch render.

The UI is playlist-first. The old single-track search, manual entry, and per-track YouTube candidate workflow were removed from the main app.

## Lyrics behavior

- If Genie returns synced lyrics, those are used first.
- If Genie does not provide synced lyrics, the app tries lrclib.
- If only plain lyrics are available and the selected policy allows them, they are still saved and rendered.
- Tracks with no usable lyrics are excluded from the queue and listed in the skipped panel.
- Playlist jobs keep each track's playlist-derived YouTube URL as the preferred audio source.

## Translation behavior

- Translation uses the OpenAI Responses API with structured output validation.
- The selected translation model is stored in `data/config/config.json`.
- Song-level translation cache is stored in `data/cache/translation_cache.json`.

## Requirements

- Python 3.11 or newer recommended
- FFmpeg available on `PATH`
  - a local bundled FFmpeg binary is already supported by the project
- An OpenAI API key

Optional:

- `spotdl` for fallback audio when a non-playlist flow reuses the pipeline directly

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
OPENAI_API_KEY=your_openai_api_key
```

## Run

```bash
python main.py
```

## Output locations

- temporary files: `data/temp`
- lyric files: `data/lyrics`
- rendered video and XML: `data/output`

## Notes

- Playlist analysis replaces the currently displayed queue.
- Batch renders are grouped into a timestamped output folder.
- If YouTube extraction becomes unreliable on your machine, update `yt-dlp` first.
