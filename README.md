# Snipr

Snipr watches streamers you choose, finds the moments worth clipping in their finished streams, renders them as captioned vertical clips, and holds them in a review queue until you approve them. It runs on your own PC.

The full design is in [`docs/design.md`](docs/design.md). What changed in each version is in [`CHANGELOG.md`](CHANGELOG.md).

## Run it

From this folder:

```
python -m uv run snipr serve --open
```

This starts the web page at `http://127.0.0.1:8765`, the background worker and the scheduler, and opens the page in your browser. Leave the window open while Snipr should be working. Press Ctrl+C to stop it.

The first run on a new machine downloads the speech-to-text model (about 1.5 GB) the first time a stream is processed, and the voice model (about 340 MB) the first time a voiceover is made.

## What needs you

- **Anthropic API key.** Paste it on the Settings page. Without one, Highlights mode still works on chat and loudness alone; Informative mode, Custom mode and automatic narration need the key.
- **Posting.** Uploading is by hand for now: at each posting slot Snipr reminds you, and the Publishing page gives you the clip file and the text to copy.

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
