# Snipr: Design Document

Revised 2026-11-20. This version folds in the decisions from the design review session. Section 16 lists what changed from the first draft.

## 1. Overview

Snipr is a personal tool that runs on your own PC. It watches a list of streamers you choose, finds the moments you care about in their streams, turns them into short-form clips, and publishes them to your content accounts after you approve each one.

## 2. Goals and non-goals

**Goals (v1)**
- Add any streamer by pasting their channel link
- Process finished VODs, one source platform per streamer. Twitch and YouTube are built; Kick follows
- Limit a streamer to chosen games, so only streams (or parts of streams) in those games are processed
- Three selection modes per streamer: highlights, informative, custom
- Produce captioned vertical (9:16) clips with an on-screen headline and a generated title, description and hashtags
- Hold every clip in an approval queue, approved one at a time
- Publish approved clips to named destinations (your content accounts on YouTube, TikTok and Facebook), on a posting schedule per destination

**Non-goals (v1)**
- Real-time clipping during a live stream
- Fully unattended posting
- Multiple users, logins or billing
- Cloud hosting
- Languages other than English
- Face or action tracking when reframing
- Configurable caption styles

**Sizing.** v1 is sized for about 5 streamers with roughly one VOD each per day.

## 3. Architecture

```
Scheduler
   |  new VOD detected (any time of day)
   v
Ingest --> Analyze --> Select --> Produce --> Review --> Publish
(yt-dlp)   (Whisper,   (scoring,  (ffmpeg,    (approval  (export, then
           chat,       Claude)    headline,   queue)     platform APIs,
           audio)                 captions,              on posting slots)
                                  metadata)
```

Everything runs locally except the LLM calls and the uploads. Each stage reads and writes job state in the database, so a failed stage can be retried without redoing the earlier ones.

**Processing window.** Detecting new VODs runs all day. The heavy stages (download, transcription, encoding) run only inside a time window you set, 01:00 to 08:00 by default, so they do not compete with you for the PC. A "run now" button starts a job outside the window.

**Failures.** A failed stage retries automatically up to 3 times with a delay. After that it stops, shows the error on the web page, and waits for a manual retry.

## 4. Streamers

Streamers are managed from the local web page, not from a config file. Any streamer can be added; there is no permission step.

**Add streamer form**

| Field | Input | Notes |
|---|---|---|
| Channel link | Text | For example `https://twitch.tv/name` |
| Platform | Dropdown: Twitch, YouTube, Kick | Filled in automatically from the link, can be changed. Only platforms whose ingest is built are selectable |
| Mode | Dropdown: Highlights, Informative, Custom | See section 5 |
| Instructions | Text box | Required for Custom, optional for Informative |
| Games | Text, comma separated | Optional. Only streams, or parts of streams, in these games are processed. See section 5 |
| Max clips per VOD | Number | Default 5 |
| Clip length | Min and max seconds | Default 20 to 60 |
| Layout | Dropdown: Fill frame, Stacked | Default Fill frame. See section 6 |
| Crop area | Rectangle marked on a sample frame | Centre crop until set. Stacked needs two rectangles: facecam and gameplay |
| Default destination | Dropdown of destinations | See section 8, can be left empty |
| Enabled | Toggle | Pause a streamer without deleting them |

Language is stored per streamer and is fixed to English in v1.

**One platform per streamer.** Each streamer has exactly one source link. If they simulcast to several platforms, pick the one with the most active chat, since chat activity drives highlight detection. Adding the same person twice on two platforms would produce duplicate clips, so the form warns when a name looks like an existing entry.

## 5. Selection modes

| Mode | What it finds | Main signals |
|---|---|---|
| Highlights | Hype, funny or high-energy moments | Chat spikes, audio energy, then an LLM pass |
| Informative | Self-contained explanations | Transcript, LLM |
| Custom | Whatever you describe in your own words | Transcript, LLM, your instructions |

### Game filter

A streamer can be limited to one or more games, for example "GTA 6". The filter runs before anything is downloaded and stacks with the selection mode: it decides what gets processed, the mode decides which moments are picked.

| Platform | How a stream is matched | Mixed streams |
|---|---|---|
| Twitch | Twitch marks the points where the streamer changes game. Each marked stretch is matched by name | Only the matching stretches are transcribed and clipped |
| YouTube | Stream replays carry no game markers, so the stream title is matched | The whole stream is processed when the title matches |

A stream with no match is skipped and logged on the Activity page. Names are matched loosely: "GTA 6" matches "Grand Theft Auto VI".

### What each platform provides

Checked against real channels on 2026-11-20.

| | Twitch | YouTube |
|---|---|---|
| Game markers | Yes | No |
| Chat replay | Yes | No |
| Highlights mode runs on | Chat and loudness spikes, then the LLM | Loudness spikes, then the LLM |

### How many clips per VOD

Two settings decide the count:

- **Cap:** "Max clips per VOD" on the streamer, default 5.
- **Quality bar:** a minimum score a moment must reach to become a clip.

A quiet stream may produce 1 or 2 clips, and a strong one stops at the cap.

### Highlights

1. Split the VOD into sliding windows (for example 30 seconds with a 10 second step).
2. Score each window as a weighted sum of the chat rate spike and the audio energy spike, both relative to that stream's baseline.
3. Take about the top 15 windows and merge neighbors. These are nominations, not clips.
4. Send the nominated windows with their transcript to the LLM in one request. It drops junk (raids, sub trains, ads, loud moments with no content), scores what is left, and sets start and end points at sentence breaks.
5. Keep the best segments above the quality bar, up to the cap. The output has the same shape as the other two modes.

### Informative

1. Send the full timestamped transcript to the LLM in a single request. Split it in two only if the stream is too long for the model's context window.
2. Ask for self-contained segments where the streamer explains something, returned as JSON with `start`, `end`, `title`, `reason` and a `score`.
3. Validate the timestamps and discard segments below the quality bar or outside the length limits.

### Custom

You type what you want, for example:

- "Moments where he reacts to a jump scare"
- "Any time she gives advice about growing a channel"
- "Arguments with teammates, but skip anything with slurs"

The flow is the same as Informative, except your text replaces the built-in instruction. The transcript is sent with per-minute chat rate and loudness figures, so instructions about reactions or hype can use those signals too.

Informative is in effect a preset of Custom with a ready-made instruction. Instructions are saved per streamer and can be edited at any time. A "test on last VOD" button reruns selection only, so you can refine the wording without downloading or transcribing again.

### Content flags

In every mode the LLM also flags segments that contain slurs, sexual content, or copyrighted music playing. A flag shows as a warning badge in review. It never blocks a clip; you decide per clip.

## 6. Clip format

Every clip is 9:16 and carries two kinds of on-screen text.

```
+------------------+
|    HEADLINE      |   short hook, on screen for the whole clip
|                  |
|                  |
|      video       |
|                  |
|                  |
|  spoken captions |   word by word, lower third
|                  |
+------------------+
```

**Layouts**

| Layout | What it does | When to use |
|---|---|---|
| Fill frame (default) | One crop rectangle, marked once per streamer, scaled to fill the whole frame | Most content, and informative content where a facecam is not needed |
| Stacked (opt-in) | Facecam rectangle on top, gameplay rectangle below | Highlights streamers where the reaction is the point |

Until a crop rectangle is marked, Fill frame uses a centre crop. The layout can be changed per clip in review. There is no face or action tracking in v1.

**Headline.** A short hook of about 6 words, for example "Getting 5 stars in GTA 6" or "How to get X". The LLM writes it as its own field, separate from the post title, and it can be edited in review.

**Spoken captions.** Word-by-word captions in the lower third, with the current word marked. There are two styles, chosen per streamer and changeable per clip:

| Style | Look |
|---|---|
| Box (default) | Up to three words on a line in white with a dark outline; the current word sits on a purple box. The Designer's spec |
| Bold | One or two heavy capitals at a time in white with a thick outline; the current word turns yellow. No box |

**Headline switch.** The headline can be turned off per streamer or per clip. With the headline off and the Bold style, a Fill frame clip is just the footage with a caption near the bottom.

Captions always stay above the bottom 380 pixels, where the apps draw the account name and description over the video.

**Source footage.** Snipr does not download the whole VOD. It downloads only the audio for analysis, then fetches the picture for each chosen clip at 1080p with 20 seconds of padding on each side. A 24 hour stream needs about 1 GB instead of tens of GB. The padding is what lets a clip be extended in review; moving a cut beyond it triggers another short download. Each fetched piece is lined up with the analysis audio by its sound, so cuts and captions are frame-exact. Snipr checks free disk space before processing a stream.

**Stacked proportions.** The facecam takes the top 1080 x 608 and the gameplay the bottom 1080 x 1312. In this layout the headline sits centred on the seam between them so it never covers the face. Crop rectangles are locked to the shape of their slot while being marked.

### Voiceover (planned, milestone 7)

An option per streamer, changeable per clip. With voiceover on, a clip is narrated instead of carrying only the streamer's own sound:

1. **Script.** The LLM writes a short narration for the chosen moment, sized to the clip's length. It gets the clip's transcript and a few still frames from the clip, so it can describe what is on screen.
2. **Voice.** A text-to-speech model that runs on this PC reads the script. The candidate is Kokoro (open source, Apache 2.0 licence, runs on the CPU), to be confirmed by testing on this machine. No account and no cost per clip.
3. **Mix.** The stream's own sound is lowered under the narration, or muted. Captions follow the narrator's words.

Voiceover needs the LLM, so it cannot run without an API key. It is the one place where still frames, not only text, are sent to the LLM.

## 7. Review

The approval queue lives on the local web page. Every clip is approved or rejected individually; there is no bulk approve.

- **Approve:** the clip is queued for each chosen destination (see section 8).
- **Edit:** start and end, headline, title, description, hashtags, layout and destinations. Changing start, end or layout re-renders the clip.
- **Reject:** offers optional one-click reasons: boring, bad cut, wrong topic, bad framing, other.

**How review decisions feed back.** Each time a stream is processed, the LLM is shown the streamer's latest approved clips and latest rejected clips with their reasons (up to eight of each), and told to lean toward the first kind and away from the second. "Bad cut" tells it to take more care over where clips start and end. "Bad framing" is left out, because it is about the crop and not about which moment was picked. This is steering by example, not training: it applies from the next stream processed, only per streamer, only when an API key is stored, and a strong moment can still be picked even if it resembles a rejected one. Without the LLM, decisions are recorded but nothing uses them.

Each clip shows its score, the LLM's reason for picking it, and any content flags.

**Source retention.** The audio and the padded footage for each clip are kept until every clip from the stream has been approved or rejected, so a clip can still be extended in review. They are deleted after that, or when they reach a maximum age. Transcripts are kept, which is what makes "test on last VOD" possible later.

**Keyboard.** A approves, E edits, R rejects, and the arrow keys move through the queue. "Approve and post now" sits next to Approve. After an approve or reject there is a brief undo.

**Sample frame for marking crops.** The add-streamer form takes one frame from the channel's latest finished stream. A channel with no finished stream yet can still be added and uses the centre crop until a frame exists.

**Alerts.** Snipr shows one Windows notification per processed VOD, with the number of clips waiting for review.

## 8. Destinations and publishing

A destination is one of your content accounts with a name you choose, for example:

| Name | Platform |
|---|---|
| Funny clips | YouTube |
| Funny clips | TikTok |
| Informative | YouTube |

- Each streamer can have a default destination, so clips can go to a dedicated account or a shared one.
- The default can be changed per clip in the approval queue, and a clip can be sent to more than one destination.
- Destinations are added in settings by signing in to the account once.
- Login tokens and the Anthropic API key are stored in Windows Credential Manager. The database holds only a reference.

**Posting slots.** An approved clip does not upload straight away. It waits for the destination's next free posting slot. Each destination has 2 slots a day by default, at 12:00 and 18:00 local time, editable per destination. A "post now" button on a clip skips the wait.

**Credit.** Every description gets a credit line with the streamer's name and channel link appended automatically. It can be edited in review.

**Two ways to post**

| Method | How it works | Built in |
|---|---|---|
| Export | Snipr gives you the clip file and copyable title, description and hashtags. You upload by hand and press "mark as posted" | Milestone 5, first |
| API upload | Snipr uploads through the platform API and records the post URL | Milestone 5 for YouTube, milestone 8 for TikTok and Facebook |

Export comes first so that publishing never depends on a platform approving an API app.

**Method per destination.** Each destination has its own posting method, set in settings. A destination starts on Export. Once the platform has approved API access and you have signed in to the account, you switch it to API upload, and from then on approved clips post themselves at their slots. Destinations switch independently, so YouTube can be automated while TikTok and Facebook are still posted by hand. The review queue, posting slots and statuses behave the same for both methods.

**Posting reminders.** When a slot comes up for a destination on Export, Snipr shows a Windows notification that a clip is due, because an export does nothing until you act on it. Opening it shows the clip file and the copyable title, description and hashtags. After uploading by hand you press "mark as posted" and can paste the post link so Snipr keeps a record.

**Status per destination.** Each clip and destination pair has its own status, so a clip can be posted on one account and failed on another, and each can be retried separately.

**First destination.** Milestones 5 and 6 run against a single YouTube account. Accounts can be created and connected at any time before milestone 5.

## 9. Components

| Stage | Responsibility | Tools |
|---|---|---|
| Scheduler | Poll enabled streamers for new VODs, enqueue jobs, hold heavy jobs for the processing window, release posts on their slots | APScheduler |
| Ingest | Apply the game filter, check disk space, download the audio and the Twitch chat replay; later fetch the footage for each clip | `yt-dlp`, Twitch's chat replay endpoint |
| Analyze | Produce a timestamped transcript with word timings, chat message rate and audio loudness curve | `faster-whisper`, `ffmpeg`, `numpy` |
| Select | Pick candidate segments with start/end times, a reason, a score and content flags | Scoring function, Claude via the Anthropic API |
| Produce | Cut, reframe to 9:16, burn in headline and captions, write metadata | `ffmpeg` with NVENC, Pillow for the text, Claude |
| Review | Show candidates, collect approve / edit / reject, choose destinations, notify | Local web page, Windows notifications |
| Publish | Export for manual upload, or upload through the API, and record the post URL | YouTube Data API, TikTok Content Posting API, Facebook Graph API |

## 10. LLM provider

**Two providers, chosen in Settings: Claude (Anthropic) or Gemini (Google).** Each has its own key, kept in Windows Credential Manager, and its own model name. A "Test connection" button sends one tiny request to confirm the key and model work. The providers share the prompts and the answer checking; only the request itself differs, so another provider can be added without touching the rest of the code.

| | Claude | Gemini |
|---|---|---|
| Default model | `claude-haiku-5-5` | `gemini-3.8-flash` |
| Cost | Paid per use from prepaid credit; cents per stream | Has a free tier with daily limits |
| What is sent used for training | No | On the free tier, yes (per Google's pricing page on 2026-11-20). Not on the paid tier |

**No key at all** still works for Highlights, on chat and loudness alone. Informative and Custom refuse to start without a key and say so. Clips can also be picked by hand: `snipr nominate` writes a stream's candidate moments with their transcript, a person or an assistant chooses, and `snipr import-picks` puts the choices in the review queue.

The rest of this section describes the Claude setup.

- **Setup:** create an Anthropic Console account, add prepaid credit, and generate an API key. Snipr stores it in Windows Credential Manager.
- **Model:** start with `claude-haiku-5-5`. Move to a Sonnet-class model if selection quality is not good enough.
- **Usage per VOD:** one or two selection requests plus one short metadata request per clip (headline, title, description, hashtags). Only text is sent, never video.
- **Cost control:** log tokens used per VOD, and set a monthly spend limit in the Console.

## 11. Hardware plan (RTX 2050, 4 GB VRAM)

| Task | Choice | Reason |
|---|---|---|
| Transcription | `faster-whisper`, `medium` model, `int8_float16` | Fits in 4 GB with room to spare. `large-v3` at int8 may fit but is tight. Drop to `small` if speed matters more than accuracy. |
| Selection LLM | Claude via API | Models small enough for 4 GB are unreliable at this task |
| Video encoding | `ffmpeg` with `h264_nvenc` | Uses the GPU encoder, much faster than CPU encoding |
| Concurrency | One GPU job at a time | Transcription and encoding should not compete for VRAM |

**Measured in milestone 1:** `medium` at `int8_float16` transcribes about 30 times faster than real time on this GPU (12 minutes of audio in 24 seconds), so a 3 hour stream takes about 6 minutes and a 24 hour stream under an hour. A one minute clip renders in about 40 seconds. A typical stream finishes well inside the processing window, so `medium` stays.

Because Snipr runs on a personal PC, the scheduler starts with Windows and catches up on any VODs published while the PC was off. Twitch deletes VODs after a retention period, so a long gap can mean missed streams.

## 12. Data model

**streamers**: `id`, `name`, `platform`, `channel_url`, `language`, `mode`, `instructions`, `max_clips_per_vod`, `clip_min_s`, `clip_max_s`, `layout`, `crop_rect`, `facecam_rect`, `default_destination_id`, `enabled`

**destinations**: `id`, `name`, `platform`, `account_handle`, `post_method`, `token_ref`, `post_slots`, `enabled`

**vods**: `id`, `streamer_id`, `source_url`, `title`, `duration`, `status`, `file_path`, `attempts`, `error`, `created_at`

**clips**: `id`, `vod_id`, `start_s`, `end_s`, `score`, `reason`, `flags`, `layout`, `file_path`, `headline`, `title`, `description`, `hashtags`, `status`, `reject_reason`

**posts**: `id`, `clip_id`, `destination_id`, `method`, `status`, `scheduled_at`, `posted_at`, `post_url`, `attempts`, `error`

### Clip status lifecycle

```
candidate -> rendered -> pending_review -> approved
                              |  ^
                              |  +-- edit (re-render)
                              +-> rejected
```

### Post status lifecycle (one per clip and destination)

```
scheduled -> posting -> posted
                |
                +-> failed (3 automatic retries, then manual)

scheduled -> exported -> posted   (reminder at the slot, manual upload, "mark as posted")
```

## 13. Global settings

```yaml
transcription:
  model: medium
  compute_type: int8_float16

llm:
  provider: anthropic
  model: claude-haiku-5-5

selection:
  min_score: 0.6
  highlight_nominations: 15

processing:
  window_start: "01:00"
  window_end: "08:00"
  max_retries: 3

ingest:
  video_height: 1080

publishing:
  require_approval: true
  default_post_slots: ["12:00", "18:00"]
  append_streamer_credit: true

storage:
  keep_vod_until_reviewed: true
  vod_max_age_days: 5
  keep_rejected_clips_days: 7
```

## 14. Tech stack

- **Language:** Python 3.11 or later, environment managed with `uv`
- **Database:** SQLite
- **Job queue:** a database-backed worker loop, one job at a time
- **Web page:** FastAPI serving on `localhost` only, no login
- **LLM:** Anthropic Python SDK
- **Secrets:** Windows Credential Manager through the `keyring` library
- **Storage:** local disk
- **Version control:** git
- **Tests:** on the pure selection logic (scoring, timestamp validation, merge rules) from milestone 1. Downloads and encoding are not unit tested.

## 15. Milestones

| # | Deliverable | Done when |
|---|---|---|
| 1 | Script: Twitch VOD URL in, transcript and 5 candidate clips out | Clips are watchable files, transcription speed is measured |
| 2 | Selection tuning for all three modes, content flags | At least half the candidates are ones you would post |
| 3 | Fill frame and Stacked layouts, headline, spoken captions | Output looks native on a phone |
| 4 | Database, streamer form, approval queue with edit and reject reasons, notifications | You can add a streamer by link and approve, edit or reject each clip |
| 5 | Destinations with a posting method each, posting slots, export with reminders | An approved clip triggers a reminder at its slot and can be marked as posted |
| 6 | Scheduler, processing window, retries | New VODs are processed with no manual step |
| 7 | Voiceover: narration script, local voice, audio mix, captions on the narrator | A clip plays with a narrated voice and matching captions |
| 8 | Kick ingest (YouTube ingest was built with milestone 1) | A Kick streamer produces clips |
| 9 | YouTube, TikTok and Facebook API upload | An approved clip posts itself on all three platforms |

## 16. Risks

| Risk | Mitigation |
|---|---|
| Copyright strikes, takedowns or bans. Streamers are clipped without asking, by decision | Per-clip approval, automatic credit line, content flags for music. The risk to the accounts is accepted |
| Platform API access is slow or restricted. Uploads from unaudited apps may be forced to private on YouTube and TikTok (not verified against current rules) | Export path ships first. Apply for TikTok and Facebook app review during milestone 1 |
| Poor clip quality | Per-clip approval, one-click rejection reasons to tune scoring and instructions |
| Custom instructions too vague to match anything | "Test on last VOD" button, show the LLM's reason for each pick |
| LLM spend creeps up | Small model by default, per-VOD token log, monthly limit in the Console |
| 4 GB VRAM runs out | One GPU job at a time, fall back to the `small` model |
| PC is off when a stream ends | Catch-up on startup, alert when a VOD is close to expiring |
| Disk usage, with 1080p sources kept until review is done | Disk check before download, maximum VOD age, cap retention for rejected clips |
| Kick chat replay may not be obtainable (not verified) | Check before milestone 7. Without chat, Kick streamers use Informative or Custom, or Highlights on audio alone |

## 17. Decisions deferred

1. Which accounts to create and how streamers map to them (dedicated, shared, or split by funny and informative). Decided once clips are flowing to the first YouTube account.
2. The order of YouTube and Kick ingest in milestone 7.
3. Whether to move to a different LLM provider after testing.

## 18. Changes from the first draft

| Area | First draft | Now |
|---|---|---|
| Platforms | Twitch, YouTube and Kick with no order | Twitch and YouTube built together, Kick in milestone 7 |
| Game filter | None | Per streamer; skips streams and parts of streams in other games |
| Download | Whole VOD at 1080p | Audio for analysis, then padded 1080p footage per clip |
| Highlights | Chat and audio spikes only | Spikes nominate, an LLM pass filters and trims |
| Reframing | "Reframe to 9:16", unspecified | Fill frame from a marked crop by default, Stacked opt-in |
| On-screen text | Captions | Headline plus word-by-word spoken captions |
| Source VOD | Deleted after render | Kept until its clips are all reviewed |
| Source quality | Unspecified | 1080p with a disk check |
| Review | Approve, edit, reject | Same, per clip only, with one-click rejection reasons, content flags and a Windows notification |
| Posting | On approval | On the destination's next slot, with "post now" |
| Publishing | API only | Export for manual upload first, API on top. Method is set per destination, with a reminder at each slot while on Export |
| Description | Generated | Generated, with an automatic streamer credit |
| Status | One status per clip | Clip status plus one status per destination |
| Processing | Whenever a VOD appears | Heavy jobs inside a time window, with "run now" |
| Failures | Retryable | 3 automatic retries, then manual |
| Secrets | Environment variable and local tokens | Windows Credential Manager |
| Language | Unstated | English only |
