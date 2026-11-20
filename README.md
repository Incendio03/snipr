# Snipr

Snipr watches streamers you choose, finds the moments worth clipping in their finished streams, renders them as captioned vertical clips, and holds them in a review queue until you approve them. It runs on your own PC.

The full design is in [`docs/design.md`](docs/design.md). What changed in each version is in [`CHANGELOG.md`](CHANGELOG.md).

## Run it

Double-click **Start Snipr.cmd** in this folder, or run:

```
python -m uv run snipr serve --open
```

This starts the web page at `http://127.0.0.1:8765`, the background worker and the scheduler, and opens the page in your browser. Leave the window open while Snipr should be working. Press Ctrl+C to stop it.

The first run on a new machine downloads the speech-to-text model (about 1.5 GB) the first time a stream is processed, and the voice model (about 340 MB) the first time a voiceover is made.

## What needs you

- **Anthropic API key.** Paste it on the Settings page. Without one, Highlights mode still works on chat and loudness alone; Informative mode, Custom mode and automatic narration need the key.
- **Posting.** Uploading is by hand for now: at each posting slot Snipr reminds you, and the Publishing page gives you the clip file and the text to copy.

## Picking clips without an API key

Snipr can find where a stream gets loud or chat speeds up on its own, but judging which of those moments are worth posting needs someone to read what was said. With an API key Claude does that automatically. Without one, there are two ways:

- **Ask Claude Code.** Open Claude Code in this folder and say, for example, "pick clips from the latest stream of IShowSpeed" or paste a stream link. It runs `snipr nominate`, reads the candidate moments, chooses the good ones, writes the headline and post text, and puts them in your Review queue with `snipr import-picks`. This uses your Claude plan, not API credit, and only happens when you ask.
- **Do it yourself.** Run `python -m uv run snipr nominate <stream link> --streamer-id 1`. It writes `nominations.txt` in the stream's folder under `data/vods`. Put your choices in a JSON file (a list of `start`, `end`, `score`, `title`, `headline`, `description`, `hashtags`) and run `python -m uv run snipr import-picks <stream link> picks.json --streamer-id 1`.

Either way the clips arrive in Review like any others, where you still approve or reject each one.

## Requirements

- Windows with an NVIDIA GPU (built and measured on an RTX 2050, 4 GB)
- Python with `uv` (`python -m pip install --user uv`), which installs everything else
- `ffmpeg` on the PATH

## Other commands

| Command | What it does |
|---|---|
| `python -m uv run snipr clip <stream link>` | Process one stream from the command line, without the web page |
| `python -m uv run snipr set-key` | Store the API key without opening the page |
| `python -m uv run pytest` | Run the tests |

## Where things are kept

Everything Snipr creates lives in the `data` folder: the database, downloaded audio and footage, rendered clips, and models. Settings are in `data/settings.yaml`. The API key is in Windows Credential Manager, not in a file.
