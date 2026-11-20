# Changelog

Each milestone in `docs/design.md` gets a version bump and a git tag: milestone N is `v0.N.0`.

## Unreleased

- **Gemini as a second AI provider.** Settings has a provider choice (Claude or Gemini), a key for each, a model name for each, and a "Test connection" button. Gemini has a free tier; on it Google uses what is sent to improve its products. Not yet run against the live Gemini API, because no key is stored; request and error handling are tested with a stand-in.
- **Picking clips by hand.** `snipr nominate` writes a stream's candidate moments with their transcript; `snipr import-picks` puts chosen clips in the review queue. Used for the first set of clips from a 24 hour stream.
- **Review decisions feed back.** A streamer's latest approvals and rejections, with reasons, are shown to the AI when the next stream is selected.
- **Cancel** on the Activity page for jobs that are not running.
- **Lost connections are waited out.** A job that fails while the PC is offline retries every two minutes without using up its attempts.
- **Transcription keeps its progress.** It is saved after every half hour of audio and never redone; only missing stretches are transcribed. Highlights transcribes only around its spikes.
- Informative and Custom stop at once, with an explanation, when no AI key is saved, instead of transcribing first.
- The sample frame follows the streamer's game filter. Changing a streamer's channel link counts as a fresh start.
- `Start Snipr.cmd`: double-click to start.

## v0.7.0 — milestone 7: voiceover

- A clip can be narrated: a voice reads a script over the footage and the captions follow the narrator.
- The voice is Kokoro, an open-source model that runs on this PC. No account, no cost per clip. Seven voices to choose from; a 30-word narration is spoken in about 5 seconds.
- The script is drafted by Claude from the clip's transcript and four still frames, or typed by hand in the edit screen (typing needs no API key).
- The stream's own sound is lowered under the narrator, or muted. When the narration is shorter than the clip the stream sound comes back afterwards.
- Checked on a real clip through the app: the script is heard in full and the captions match it. **Automatic script writing has not been run live** (no API key stored).

## v0.6.0 — milestone 6: scheduler, processing window, retries

- New streams are found automatically every 15 minutes, with a catch-up check on start. A new streamer starts with only their latest stream, not a backlog.
- Heavy work waits for the processing window (01:00 to 08:00 by default); "Run now" overrides it.
- A failed job retries up to 3 times with a growing delay, then stops and shows its error with a Retry button.
- Footage and audio are cleaned up once a stream's clips are all reviewed. Transcripts are kept.

## v0.5.0 — milestone 5: destinations and export

- Destinations with their own posting slots; an approved clip takes the destination's next free slot, one clip per slot.
- At the slot, a Windows notification says the clip is due and the Publishing page gives the file and the text to copy. "Post now", "Mark as posted" with an optional link, and Cancel.
- Automatic upload through the platforms' APIs is not built (milestone 9).

## v0.4.0 — milestone 4: database, web app, review queue

- The web page at `http://127.0.0.1:8765`: Review, Edit clip, Streamers, Add streamer with crop marking, Publishing, Activity and Settings, built from the Designer's mock.
- Add a streamer by pasting a channel link. Approve, edit or reject each clip, with one-click rejection reasons, keyboard shortcuts and undo.
- Editing a clip's start, end, headline or layout re-renders it; moving a cut beyond the saved footage downloads more.
- Game filter per streamer. Caption style per streamer and per clip: Box (the Designer's spec) or Bold (heavy capitals, spoken word in yellow), and a switch to hide the headline.
- The sample frame for marking crops comes from a recent stream that matches the streamer's games, and skips a stream the platform is still processing. Fixed frames not being readable from YouTube streams.
- Checked in a real browser: a scripted run in Edge clicks through review, edit, approve, undo, post now, mark as posted, reject and the other screens against the real server. 16 checks, no console errors.
- Checked end to end on a real stream: 45 minutes of one game out of a 3 hour stream went from link to three clips in the review queue in about 9 minutes.
- Without an API key, Highlights picks on chat and loudness alone with its own quality bar, and leaves the headline empty.

## v0.3.0 — milestone 3: vertical reframing, headline and captions

- Clips render at 1080 x 1920: Fill frame (one 9:16 crop, centre by default) or Stacked (facecam 1080 x 608 above gameplay 1080 x 1312).
- Headline plate and word-by-word captions drawn to the Designer's clip format spec, with the current word on the purple box. Chakra Petch and Inter are bundled.
- Crop rectangles are stored as fractions of the frame and fitted to the slot's aspect ratio, so they work at any source resolution.
- Checked on real footage in both layouts. A 56 s clip renders in about 37 s on the RTX 2050.
- Word fragments from the transcript ("y" + "'all") are joined before captioning.

## v0.2.0 — milestone 2: selection for all three modes

- Claude provider behind two functions, `select_segments` and `write_metadata`; model `claude-haiku-5-5`.
- Highlights: spikes nominate up to 15 windows, then one LLM request filters and trims them.
- Informative and Custom: the whole transcript goes in one request with per-minute chat and loudness figures. Transcripts too long for one request are split and merged.
- Content flags (slurs, sexual content, copyrighted music) come back with each segment.
- Headline, title, description and hashtags are written per clip.
- `snipr set-key` stores the API key in Windows Credential Manager.
- **Not yet run against the live API**: no key is stored on this machine. Prompt building, response validation and error handling are covered by tests with a stand-in client. The milestone's own bar ("at least half the candidates are ones you would post") still needs a real run and your judgement.

## v0.1.0 — milestone 1: VOD link in, transcript and candidate clips out

- `snipr clip <vod link>` downloads the audio, reads Twitch chat replay, transcribes with word timings and cuts candidate clips.
- Works on Twitch VODs and YouTube stream replays. Twitch gives game chapters and chat; YouTube gives neither.
- Game filter (`--game "GTA 6"`): only the matching chapters of a stream are processed, or the VOD is skipped.
- Only the audio is downloaded for analysis. The picture is fetched per clip with 20 s of padding, instead of the whole VOD at 1080p.
- Measured on the RTX 2050: `medium` at `int8_float16` transcribes about 30x real time (12 minutes in 24 s).
- Clip cuts are frame-exact against the transcript timeline (checked by audio alignment, 0.00 s error on four clips).
- Without an LLM key, Highlights picks on chat and loudness spikes alone.
- Project scaffold: `uv` environment on Python 3.12, dependencies, design document.
