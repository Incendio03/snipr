from snipr import db, worker


def _streamer_body(**extra):
    body = {"channel_url": "https://twitch.tv/PixelMoth", "mode": "highlights", "instructions": "",
            "games": "GTA 6", "max_clips_per_vod": 5, "clip_min_s": 20, "clip_max_s": 60, "layout": "fill",
            "crop_rect": None, "facecam_rect": None, "gameplay_rect": None, "default_destination_id": None,
            "enabled": True}
    body.update(extra)
    return body


# ---- guard ------------------------------------------------------------------

def test_requests_from_other_sites_and_hosts_are_refused(client):
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 403
    cross = client.post("/api/run-now", headers={"origin": "https://evil.example"})
    assert cross.status_code == 403 and "Cross-site" in cross.json()["detail"]
    assert client.post("/api/run-now", headers={"origin": "http://127.0.0.1:8765"}).status_code == 200


def test_state_reports_counts_and_missing_key(client, seeded):
    state = client.get("/api/state").json()
    assert state["review_count"] == 2 and state["due_posts"] == 0 and state["llm_ready"] is False
    assert set(state["window"]) == {"mode", "start", "end", "open_now"} and state["window"]["mode"] == "manual"


# ---- streamers --------------------------------------------------------------

def test_resolve_fills_platform_and_warns_about_duplicates(client, seeded):
    found = client.post("/api/streamers/resolve", json={"channel_url": "twitch.tv/PixelMoth"}).json()
    assert found["platform"] == "twitch" and found["channel_url"] == "https://www.twitch.tv/pixelmoth"
    assert found["supported"] is True and found["duplicate_of"] == {"id": seeded["streamer"], "name": "pixelmoth"}
    youtube = client.post("/api/streamers/resolve", json={"channel_url": "https://www.youtube.com/@IShowSpeed"}).json()
    assert youtube["platform"] == "youtube" and youtube["name"] == "IShowSpeed" and youtube["duplicate_of"] is None
    kick = client.post("/api/streamers/resolve", json={"channel_url": "https://kick.com/someone"}).json()
    assert kick["supported"] is False and "not supported yet" in kick["message"]
    assert client.post("/api/streamers/resolve", json={"channel_url": "hello"}).status_code == 400


def test_create_update_and_delete_a_streamer(client):
    created = client.post("/api/streamers", json=_streamer_body())
    assert created.status_code == 201
    streamer = created.json()
    assert streamer["name"] == "pixelmoth" and streamer["platform"] == "twitch" and streamer["games"] == "GTA 6"
    assert streamer["stats"] == {"pending": 0, "approved": 0, "rejected": 0, "last_vod_title": None,
                                 "last_vod_at": None, "last_checked_at": None}
    changed = client.put(f"/api/streamers/{streamer['id']}", json={"enabled": False, "crop_rect": [0.3, 0, 0.3, 1]})
    assert changed.json()["enabled"] is False and changed.json()["crop_rect"] == [0.3, 0.0, 0.3, 1.0]
    assert len(client.get("/api/streamers").json()) == 1
    assert client.delete(f"/api/streamers/{streamer['id']}").status_code == 204
    assert client.get("/api/streamers").json() == []
    assert client.put(f"/api/streamers/{streamer['id']}", json={"enabled": True}).status_code == 404


def test_changing_the_channel_link_resets_the_first_check(client):
    streamer = client.post("/api/streamers", json=_streamer_body()).json()
    db.update("streamers", streamer["id"], {"last_checked_at": db.now()})
    same = client.put(f"/api/streamers/{streamer['id']}", json={"channel_url": streamer["channel_url"], "games": "x"}).json()
    assert same["stats"]["last_checked_at"] is not None           # same channel: nothing reset
    moved = client.put(f"/api/streamers/{streamer['id']}", json={"channel_url": "https://www.youtube.com/@PixelMoth"}).json()
    assert moved["platform"] == "youtube" and moved["channel_url"] == "https://www.youtube.com/@PixelMoth"
    assert moved["stats"]["last_checked_at"] is None              # new channel: treated as never checked


def test_streamer_validation_messages(client):
    def detail(**extra):
        response = client.post("/api/streamers", json=_streamer_body(**extra))
        assert response.status_code == 422
        return response.json()["detail"]

    assert "instructions" in detail(mode="custom")
    assert "shortest" in detail(clip_min_s=90, clip_max_s=60)
    assert "Get sample frame" in detail(layout="stacked")          # says what to do, not only the rule
    assert "inside the frame" in detail(crop_rect=[0.8, 0, 0.5, 1])
    assert "not supported yet" in detail(channel_url="https://kick.com/someone")
    assert "Mode" in detail(mode="sideways")
    ok = client.post("/api/streamers", json=_streamer_body(mode="custom", instructions="jump scares",
                                                          layout="stacked", facecam_rect=[0, 0.5, 0.4, 0.5],
                                                          gameplay_rect=[0.2, 0, 0.6, 1]))
    assert ok.status_code == 201


def test_switching_an_existing_streamer_to_custom_still_needs_instructions(client):
    streamer = client.post("/api/streamers", json=_streamer_body()).json()
    assert client.put(f"/api/streamers/{streamer['id']}", json={"mode": "custom"}).status_code == 422
    assert client.put(f"/api/streamers/{streamer['id']}",
                      json={"mode": "custom", "instructions": "advice"}).json()["mode"] == "custom"


def test_test_selection_needs_an_analysed_stream(client, seeded):
    response = client.post(f"/api/streamers/{seeded['streamer']}/test-selection")
    assert response.status_code == 409 and "analysed" in response.json()["detail"]


# ---- clips ------------------------------------------------------------------

def test_review_queue_lists_clips_with_everything_the_page_needs(client, seeded):
    clips = client.get("/api/clips").json()
    assert [c["id"] for c in clips] == seeded["clips"]            # best score first within a stream
    clip = clips[0]
    assert clip["streamer_name"] == "pixelmoth" and clip["duration_s"] == 41.0 and clip["min_score"] == 0.6
    assert clip["video_url"] == f"/media/clips/{clip['id']}.mp4?v=0" and clip["render_state"] == "ready"
    assert clip["source_range"] == {"start_s": 8028.0, "end_s": 8109.0}
    assert clip["layout"] == {"type": "fill", "crop": None, "facecam": None, "gameplay": None,
                              "caption_style": "box", "show_headline": True,
                              "voiceover": False, "voice": "am_michael", "mix": "duck"}
    assert clip["narration"] == ""
    assert client.get("/api/clips", params={"streamer_id": 999}).json() == []
    assert client.get("/api/clips", params={"status": "nonsense"}).status_code == 400
    assert client.get(f"/media/clips/{clip['id']}.mp4").content == b"not really a video"


def test_editing_text_does_not_rerender_but_editing_the_cut_does(client, seeded):
    clip_id = seeded["clips"][0]
    text = client.put(f"/api/clips/{clip_id}", json={"title": "New title", "hashtags": ["#GTA6", "gta6", "Fun Clips"]}).json()
    assert text["title"] == "New title" and text["hashtags"] == ["gta6", "funclips"] and text["render_state"] == "ready"
    assert db.scalar("SELECT COUNT(*) FROM jobs") == 0

    cut = client.put(f"/api/clips/{clip_id}", json={"start_s": 8040.0, "end_s": 8085.0}).json()
    assert cut["render_state"] == "rendering" and cut["video_url"] is None and cut["duration_s"] == 45.0
    job = db.row("SELECT * FROM jobs")
    assert job["kind"] == "render_clip" and job["ref_id"] == clip_id and job["force"] == 1
    assert client.post(f"/api/clips/{clip_id}/approve", json={"destination_ids": []}).status_code == 409


def test_headline_and_layout_changes_rerender(client, seeded):
    first, second = seeded["clips"]
    assert client.put(f"/api/clips/{first}", json={"headline": "A new hook"}).json()["render_state"] == "rendering"
    same = client.put(f"/api/clips/{second}", json={"layout": {"type": "fill", "crop": None}}).json()
    assert same["render_state"] == "ready"                         # nothing actually changed
    moved = client.put(f"/api/clips/{second}", json={"layout": {"type": "fill", "crop": [0.1, 0, 0.3, 1]}}).json()
    assert moved["render_state"] == "rendering" and moved["layout"]["crop"] == [0.1, 0.0, 0.3, 1.0]


def test_caption_style_and_headline_switch_on_streamers_and_clips(client, seeded):
    streamer = client.post("/api/streamers", json=_streamer_body(channel_url="https://youtube.com/@Someone",
                                                                 caption_style="bold", show_headline=False)).json()
    assert streamer["caption_style"] == "bold" and streamer["show_headline"] is False
    assert worker.streamer_layout(db.get("streamers", streamer["id"])) == {
        "type": "fill", "crop": None, "facecam": None, "gameplay": None, "caption_style": "bold",
        "show_headline": False, "voiceover": False, "voice": "am_michael", "mix": "duck"}
    assert client.put(f"/api/streamers/{streamer['id']}", json={"caption_style": "comic"}).status_code == 422

    clip_id = seeded["clips"][0]
    before = client.get(f"/api/clips/{clip_id}").json()["layout"]
    assert before["caption_style"] == "box" and before["show_headline"] is True       # defaults for older clips
    after = client.put(f"/api/clips/{clip_id}", json={"layout": {**before, "caption_style": "bold",
                                                                 "show_headline": False}}).json()
    assert after["layout"]["caption_style"] == "bold" and after["layout"]["show_headline"] is False
    assert after["render_state"] == "rendering"
    assert client.put(f"/api/clips/{clip_id}", json={"layout": {**before, "caption_style": "comic"}}).status_code == 422


def test_voiceover_settings_on_streamers(client):
    streamer = client.post("/api/streamers", json=_streamer_body(voiceover=True, voice="bm_george",
                                                                 voiceover_mix="mute")).json()
    assert (streamer["voiceover"], streamer["voice"], streamer["voiceover_mix"]) == (True, "bm_george", "mute")
    layout = worker.streamer_layout(db.get("streamers", streamer["id"]))
    assert (layout["voiceover"], layout["voice"], layout["mix"]) == (True, "bm_george", "mute")
    assert client.put(f"/api/streamers/{streamer['id']}", json={"voice": "robot"}).status_code == 422
    assert client.put(f"/api/streamers/{streamer['id']}", json={"voiceover_mix": "loud"}).status_code == 422
    voices = client.get("/api/voices").json()
    assert voices["default"] == "am_michael" and {"id": "bm_george", "label": "George (UK, male)"} in voices["voices"]


def test_a_streamers_narration_style_is_saved_and_offered_on_their_clips(client, seeded):
    sid = seeded["streamer"]
    assert client.get("/api/streamers").json()[0]["narration_style"] == ""
    saved = client.put(f"/api/streamers/{sid}", json={"narration_style": "  Hype sports commentator.  "}).json()
    assert saved["narration_style"] == "Hype sports commentator."
    assert client.get(f"/api/clips/{seeded['clips'][0]}").json()["narration_style"] == "Hype sports commentator."
    assert client.put(f"/api/streamers/{sid}", json={"narration_style": "x" * 1501}).status_code == 422
    assert client.put(f"/api/streamers/{sid}", json={"narration_style": ""}).json()["narration_style"] == ""


def test_voiceover_on_a_clip_needs_a_narration_that_fits(client, seeded):
    clip_id = seeded["clips"][0]                                   # a 41 second clip
    layout = client.get(f"/api/clips/{clip_id}").json()["layout"]
    on = {**layout, "voiceover": True}

    empty = client.put(f"/api/clips/{clip_id}", json={"layout": on})
    assert empty.status_code == 422 and "no narration yet" in empty.json()["detail"]
    long = client.put(f"/api/clips/{clip_id}", json={"layout": on, "narration": "word " * 400})
    assert long.status_code == 422 and "400 words" in long.json()["detail"]

    saved = client.put(f"/api/clips/{clip_id}", json={"layout": on, "narration": "  Speed walks  into\nthe water. "}).json()
    assert saved["narration"] == "Speed walks into the water." and saved["layout"]["voiceover"] is True
    assert saved["render_state"] == "rendering"

    db.update("clips", clip_id, {"render_state": "ready"})
    same = client.put(f"/api/clips/{clip_id}", json={"title": "Only the title"}).json()
    assert same["render_state"] == "ready"                         # text-only edit leaves the video alone
    changed = client.put(f"/api/clips/{clip_id}", json={"narration": "A different script entirely."}).json()
    assert changed["render_state"] == "rendering"

    # With voiceover off, editing the stored narration does not re-render.
    db.update("clips", clip_id, {"render_state": "ready"})
    off = client.put(f"/api/clips/{clip_id}", json={"layout": {**layout, "voiceover": False}}).json()
    db.update("clips", clip_id, {"render_state": "ready"})
    assert off["layout"]["voiceover"] is False
    assert client.put(f"/api/clips/{clip_id}", json={"narration": "Kept for later."}).json()["render_state"] == "ready"


def test_narration_templates_can_be_saved_updated_and_deleted(client):
    assert client.get("/api/narration-templates").json() == []
    hype = client.post("/api/narration-templates", json={"name": " Hype  caster ", "prompt": "Short punchy lines."})
    assert hype.status_code == 201 and hype.json()["name"] == "Hype caster"
    client.post("/api/narration-templates", json={"name": "Calm explainer", "prompt": "Slow and clear."})
    assert [t["name"] for t in client.get("/api/narration-templates").json()] == ["Calm explainer", "Hype caster"]

    # Saving under a name that exists updates that template instead of making a twin.
    again = client.post("/api/narration-templates", json={"name": "hype caster", "prompt": "Even punchier."}).json()
    assert again["id"] == hype.json()["id"] and again["prompt"] == "Even punchier."
    assert len(client.get("/api/narration-templates").json()) == 2

    assert client.post("/api/narration-templates", json={"name": "", "prompt": "x"}).status_code == 422
    assert client.post("/api/narration-templates", json={"name": "x", "prompt": " "}).status_code == 422
    assert client.post("/api/narration-templates", json={"name": "x", "prompt": "y" * 1501}).status_code == 422
    assert client.delete(f"/api/narration-templates/{again['id']}").status_code == 204
    assert client.delete(f"/api/narration-templates/{again['id']}").status_code == 404
    assert [t["name"] for t in client.get("/api/narration-templates").json()] == ["Calm explainer"]


def test_rewrite_uses_the_typed_text_the_style_and_the_unsaved_cut(client, seeded, monkeypatch):
    from snipr import llm

    clip_id = seeded["clips"][0]
    no_key = client.post(f"/api/clips/{clip_id}/rewrite-narration", json={"text": "Speed walks in."})
    assert no_key.status_code == 409 and "API key" in no_key.json()["detail"]

    seen = {}

    class Fake:
        def rewrite_narration(self, draft, clip_text, clip_seconds, word_budget, instructions, streamer):
            seen.update(draft=draft, seconds=clip_seconds, budget=word_budget, instructions=instructions, streamer=streamer)
            return "Speed walks straight in."

        def total_tokens(self):
            return {"input_tokens": 50, "output_tokens": 9, "requests": 1}

    monkeypatch.setattr(llm, "get_provider", lambda settings: Fake())
    result = client.post(f"/api/clips/{clip_id}/rewrite-narration",
                         json={"text": "  Speed   walks in. ", "instructions": "hype caster", "start_s": 8048.0, "end_s": 8078.0}).json()
    assert result == {"narration": "Speed walks straight in.", "words": 4, "word_budget": 75}
    assert seen == {"draft": "Speed walks in.", "seconds": 30.0, "budget": 75, "instructions": "hype caster",
                    "streamer": "pixelmoth"}
    assert db.get("vods", seeded["vod"])["llm_requests"] == 1                # usage is counted
    assert client.get(f"/api/clips/{clip_id}").json()["narration"] == ""     # nothing was saved

    assert client.post(f"/api/clips/{clip_id}/rewrite-narration", json={"text": "  "}).status_code == 422
    assert client.post(f"/api/clips/{clip_id}/rewrite-narration", json={"text": "x", "instructions": "y" * 1501}).status_code == 422
    assert client.post(f"/api/clips/{clip_id}/write-narration", json={"instructions": 5}).status_code == 422


def test_drafting_a_narration_needs_a_key(client, seeded):
    response = client.post(f"/api/clips/{seeded['clips'][0]}/write-narration")
    assert response.status_code == 409 and "API key" in response.json()["detail"]


def test_clip_edit_validation(client, seeded):
    clip_id = seeded["clips"][0]

    def detail(body):
        response = client.put(f"/api/clips/{clip_id}", json=body)
        assert response.status_code == 422
        return response.json()["detail"]

    assert "after the start" in detail({"start_s": 8090.0, "end_s": 8048.0})
    assert "20 to 60 seconds" in detail({"start_s": 8048.0, "end_s": 8200.0})
    assert "inside the stream" in detail({"start_s": 8990.0, "end_s": 9030.0})
    assert "Get sample frame" in detail({"layout": {"type": "stacked"}})
    assert client.put("/api/clips/9999", json={"title": "x"}).status_code == 404


def test_approve_schedules_posts_and_reject_and_restore_clean_up(client, seeded):
    dest = client.post("/api/destinations", json={"name": "Funny clips", "platform": "youtube",
                                                  "post_slots": ["12:00", "18:00"]}).json()
    first, second = seeded["clips"]

    approved = client.post(f"/api/clips/{first}/approve", json={"destination_ids": [dest["id"]], "post_now": False}).json()
    assert approved["clip"]["status"] == "approved" and len(approved["posts"]) == 1
    post = approved["posts"][0]
    assert post["status"] == "scheduled" and post["destination_name"] == "Funny clips"
    assert post["clip"]["post_text"] == "The wanted level climbs.\n\nCredit: pixelmoth"
    assert post["clip"]["hashtags_text"] == "#gta6 #gaming"
    assert client.get("/api/state").json()["review_count"] == 1

    restored = client.post(f"/api/clips/{first}/restore").json()
    assert restored["status"] == "pending_review" and db.scalar("SELECT COUNT(*) FROM posts") == 0

    rejected = client.post(f"/api/clips/{second}/reject", json={"reason": "bad_cut"}).json()
    assert rejected["status"] == "rejected" and rejected["reject_reason"] == "bad_cut"
    assert client.post(f"/api/clips/{second}/reject", json={"reason": "meh"}).status_code == 422
    assert [c["id"] for c in client.get("/api/clips", params={"status": "rejected"}).json()] == [second]


def test_review_decisions_become_history_for_the_next_selection(client, seeded):
    first, second = seeded["clips"]
    assert worker.review_history(seeded["streamer"]) == []          # nothing reviewed yet
    client.post(f"/api/clips/{first}/approve", json={"destination_ids": []})
    client.post(f"/api/clips/{second}/reject", json={"reason": "boring"})
    history = worker.review_history(seeded["streamer"])
    assert [(h["verdict"], h["reason"]) for h in history] == [("approved", None), ("rejected", "boring")]
    assert history[0]["title"] == "Five stars" and history[0]["summary"] == "The wanted level climbs."
    # Undoing a decision takes it back out, and another streamer's decisions never leak in.
    client.post(f"/api/clips/{second}/restore")
    assert [h["verdict"] for h in worker.review_history(seeded["streamer"])] == ["approved"]
    assert worker.review_history(seeded["streamer"] + 1) == []


def test_approving_with_no_destination_schedules_nothing(client, seeded):
    result = client.post(f"/api/clips/{seeded['clips'][0]}/approve", json={"destination_ids": []}).json()
    assert result["clip"]["status"] == "approved" and result["posts"] == []


# ---- destinations and posts -------------------------------------------------

def test_destination_validation_and_delete_rules(client, seeded):
    assert client.post("/api/destinations", json={"name": "", "platform": "youtube"}).status_code == 422
    assert client.post("/api/destinations", json={"name": "X", "platform": "myspace"}).status_code == 422
    assert "HH:MM" in client.post("/api/destinations", json={"name": "X", "platform": "tiktok",
                                                             "post_slots": ["noon"]}).json()["detail"]
    assert "not built yet" in client.post("/api/destinations", json={"name": "X", "platform": "youtube",
                                                                     "post_method": "api"}).json()["detail"]
    dest = client.post("/api/destinations", json={"name": "Funny clips", "platform": "tiktok"}).json()
    assert dest["post_slots"] == ["12:00", "18:00"] and dest["post_method"] == "export" and dest["api_available"] is False
    assert dest["next_free_slot"] and dest["queued"] == 0

    client.post(f"/api/clips/{seeded['clips'][0]}/approve", json={"destination_ids": [dest["id"]]})
    assert client.get("/api/destinations").json()[0]["queued"] == 1
    blocked = client.delete(f"/api/destinations/{dest['id']}")
    assert blocked.status_code == 409 and "1 unsent post" in blocked.json()["detail"]


def test_post_lifecycle_post_now_mark_posted_cancel(client, seeded):
    dest = client.post("/api/destinations", json={"name": "Funny clips", "platform": "youtube"}).json()
    first, second = seeded["clips"]
    post = client.post(f"/api/clips/{first}/approve", json={"destination_ids": [dest["id"]]}).json()["posts"][0]
    other = client.post(f"/api/clips/{second}/approve", json={"destination_ids": [dest["id"]]}).json()["posts"][0]
    assert post["scheduled_at"] != other["scheduled_at"]

    due = client.post(f"/api/posts/{post['id']}/post-now").json()
    assert due["status"] == "due" and client.get("/api/state").json()["due_posts"] == 1
    assert client.post(f"/api/posts/{post['id']}/post-now").status_code == 409
    assert [p["id"] for p in client.get("/api/posts").json()] == [post["id"], other["id"]]     # due first

    assert client.post(f"/api/posts/{post['id']}/mark-posted", json={"post_url": "not a link"}).status_code == 422
    done = client.post(f"/api/posts/{post['id']}/mark-posted", json={"post_url": "https://youtube.com/shorts/abc"}).json()
    assert done["status"] == "posted" and done["post_url"] == "https://youtube.com/shorts/abc" and done["posted_at"]
    assert client.post(f"/api/posts/{post['id']}/cancel").status_code == 409

    assert client.post(f"/api/posts/{other['id']}/cancel").status_code == 204
    assert client.get("/api/posts").json() == []
    assert [p["id"] for p in client.get("/api/posts", params={"status": "posted"}).json()] == [post["id"]]
    assert client.delete(f"/api/destinations/{dest['id']}").status_code == 204


# ---- activity and settings --------------------------------------------------

def test_activity_shows_jobs_held_for_the_window_and_run_now_releases_them(client, seeded):
    client.put("/api/settings", json={"processing": {"window_start": "00:00", "window_end": "00:01"}})
    job_id = worker.enqueue("process_vod", seeded["vod"], "pixelmoth: GTA 6 day 2")
    from datetime import datetime
    settings = __import__("snipr.config", fromlist=["x"]).load_settings()
    midday = datetime(2026, 11, 21, 12, 0)
    assert worker.job_view(db.get("jobs", job_id), settings, midday)["status"] == "waiting"
    assert worker.next_job(settings, midday) is None

    activity = client.get("/api/activity").json()
    assert activity["jobs"][0]["label"] == "pixelmoth: GTA 6 day 2" and activity["jobs"][0]["max_attempts"] == 3
    assert activity["usage"]["tokens_in"] == 0 and "since" in activity["usage"]

    assert client.post(f"/api/jobs/{job_id}/run-now").json()["status"] == "queued"
    assert worker.next_job(settings, midday)["id"] == job_id


def test_manual_is_the_default_and_a_window_is_opt_in(client, seeded):
    from datetime import datetime

    from snipr import config

    inside = datetime(2026, 11, 21, 3, 0)                              # 03:00, inside the default 01:00 to 08:00
    job_id = worker.enqueue("process_vod", seeded["vod"], "pixelmoth: GTA 6 day 2")

    settings = config.load_settings()
    assert settings["processing"]["mode"] == "manual"
    assert worker.next_job(settings, inside) is None                   # manual: nothing starts by itself, even at 03:00
    assert worker.job_view(db.get("jobs", job_id), settings, inside)["status"] == "waiting"

    saved = client.put("/api/settings", json={"processing": {"mode": "window"}}).json()
    assert saved["processing"]["mode"] == "window"
    settings = config.load_settings()
    assert worker.next_job(settings, inside)["id"] == job_id           # window mode: starts inside the hours
    assert worker.next_job(settings, datetime(2026, 11, 21, 12, 0)) is None   # and waits outside them

    client.put("/api/settings", json={"processing": {"mode": "manual"}})
    assert client.post(f"/api/jobs/{job_id}/run-now").json()["status"] == "queued"
    assert worker.next_job(config.load_settings(), inside)["id"] == job_id    # Run now works in manual mode
    assert client.put("/api/settings", json={"processing": {"mode": "always"}}).status_code == 422


def test_failed_jobs_retry_then_stop_and_can_be_retried_by_hand(client, seeded, monkeypatch):
    calls = []

    def explode(job, settings):
        calls.append(job["attempts"])
        raise RuntimeError("network down")

    monkeypatch.setitem(worker.HANDLERS, "process_vod", explode)
    settings = __import__("snipr.config", fromlist=["x"]).load_settings()
    job_id = worker.enqueue("process_vod", seeded["vod"], "pixelmoth: GTA 6 day 2", force=True)
    for _ in range(3):
        db.update("jobs", job_id, {"run_after": None})
        worker.run_job(db.get("jobs", job_id), settings)
    job = db.get("jobs", job_id)
    assert calls == [0, 1, 2] and job["status"] == "failed" and job["error"] == "network down"
    assert db.get("vods", seeded["vod"])["status"] == "failed"
    assert client.get("/api/state").json()["failed_jobs"] == 1
    retried = client.post(f"/api/jobs/{job_id}/retry").json()
    assert retried["status"] == "queued" and retried["attempts"] == 0 and retried["error"] is None


def test_a_lost_connection_does_not_use_up_attempts(data_dir, seeded, monkeypatch):
    def explode(job, settings):
        raise RuntimeError("getaddrinfo failed")

    monkeypatch.setitem(worker.HANDLERS, "process_vod", explode)
    monkeypatch.setattr(worker, "is_online", lambda: False)
    settings = __import__("snipr.config", fromlist=["x"]).load_settings()
    job_id = worker.enqueue("process_vod", seeded["vod"], "pixelmoth: GTA 6 day 2", force=True)
    for _ in range(6):                                            # far more tries than the retry limit
        db.update("jobs", job_id, {"run_after": None})
        worker.run_job(db.get("jobs", job_id), settings)
    job = db.get("jobs", job_id)
    assert job["status"] == "queued" and job["attempts"] == 0 and job["error"] == worker.OFFLINE_MESSAGE
    assert job["run_after"] is not None and job["stage"] == "Waiting for the internet"
    assert db.scalar("SELECT COUNT(*) FROM events WHERE message LIKE 'No internet connection%'") == 1   # said once

    monkeypatch.setattr(worker, "is_online", lambda: True)        # back online: failures count again
    db.update("jobs", job_id, {"run_after": None})
    worker.run_job(db.get("jobs", job_id), settings)
    assert db.get("jobs", job_id)["attempts"] == 1


def test_cancelling_a_job_removes_it_and_the_stream_is_not_queued_again(client, seeded):
    job_id = worker.enqueue("process_vod", seeded["vod"], "pixelmoth: GTA 6 day 2")
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 204
    assert db.get("jobs", job_id) is None and db.get("vods", seeded["vod"])["status"] == "ignored"
    assert client.get("/api/activity").json()["jobs"] == []
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 404

    running = worker.enqueue("process_vod", seeded["vod"], "x")
    db.update("jobs", running, {"status": "running"})
    blocked = client.post(f"/api/jobs/{running}/cancel")
    assert blocked.status_code == 409 and "running right now" in blocked.json()["detail"]

    render = worker.enqueue("render_clip", seeded["clips"][0], "Re-render")
    assert client.post(f"/api/jobs/{render}/cancel").status_code == 204
    clip = client.get(f"/api/clips/{seeded['clips'][0]}").json()
    assert clip["render_state"] == "failed" and "cancelled" in clip["render_error"]


def test_a_permanent_failure_is_not_retried(data_dir, seeded, monkeypatch):
    def explode(job, settings):
        raise worker.Permanent("Custom mode needs an API key.")

    monkeypatch.setitem(worker.HANDLERS, "process_vod", explode)
    job_id = worker.enqueue("process_vod", seeded["vod"], "x", force=True)
    worker.run_job(db.get("jobs", job_id), __import__("snipr.config", fromlist=["x"]).load_settings())
    job = db.get("jobs", job_id)
    assert job["status"] == "failed" and job["attempts"] == 1 and "API key" in job["error"]


def test_settings_round_trip_and_validation(client):
    settings = client.get("/api/settings").json()
    assert settings["llm"]["model"] == "claude-haiku-5-5" and settings["llm"]["key_set"] is False
    assert settings["processing"]["window_start"] == "01:00" and settings["storage"]["free_gb"] > 0
    saved = client.put("/api/settings", json={"processing": {"window_start": "2:30", "poll_minutes": 30},
                                              "selection": {"min_score": 0.7},
                                              "publishing": {"default_post_slots": ["20:00", "8:00"]},
                                              "transcription": {"model": "small"}}).json()
    assert saved["processing"]["window_start"] == "02:30" and saved["processing"]["poll_minutes"] == 30
    assert saved["selection"]["min_score"] == 0.7 and saved["publishing"]["default_post_slots"] == ["08:00", "20:00"]
    assert saved["transcription"]["model"] == "small"
    assert client.put("/api/settings", json={"selection": {"min_score": 4}}).status_code == 422
    assert client.put("/api/settings", json={"processing": {"window_end": "late"}}).status_code == 422
    assert client.put("/api/settings", json={"transcription": {"model": "huge"}}).status_code == 422
    assert client.post("/api/settings/api-key", json={"key": "short"}).status_code == 422


def test_gemini_can_be_chosen_and_keys_are_kept_per_provider(client, monkeypatch):
    from snipr import llm

    stored = {}
    monkeypatch.setattr(llm.secrets, "get", lambda name: stored.get(name))
    monkeypatch.setattr("snipr.web.app.secrets.put", lambda name, value: stored.__setitem__(name, value))
    monkeypatch.setattr("snipr.web.app.secrets.delete", lambda name: stored.pop(name, None))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    start = client.get("/api/settings").json()["llm"]
    assert start["provider"] == "anthropic" and start["gemini_model"] == "gemini-3.5-flash-lite"
    assert start["key_set"] is False and start["gemini_key_set"] is False and "free tier" in start["gemini_note"]

    saved = client.post("/api/settings/api-key", json={"key": "AIza-test-key-123456789", "provider": "gemini"}).json()
    assert saved == {"key_set": True, "provider": "gemini"} and "gemini_api_key" in stored
    assert client.get("/api/state").json()["llm_ready"] is False        # Claude is still the one in use

    switched = client.put("/api/settings", json={"llm": {"provider": "gemini", "gemini_model": "gemini-3.5-flash-lite"}}).json()
    assert switched["llm"]["provider"] == "gemini" and switched["llm"]["gemini_key_set"] is True
    state = client.get("/api/state").json()
    assert state["llm_ready"] is True and state["provider"] == "gemini" and state["model"] == "gemini-3.5-flash-lite"

    assert client.put("/api/settings", json={"llm": {"provider": "copilot"}}).status_code == 422
    assert client.post("/api/settings/api-key", json={"key": "x" * 30, "provider": "copilot"}).status_code == 422
    removed = client.delete("/api/settings/api-key", params={"provider": "gemini"}).json()
    assert removed == {"key_set": False, "provider": "gemini"} and client.get("/api/state").json()["llm_ready"] is False


def test_test_connection_reports_success_and_the_providers_own_error(client, monkeypatch):
    from snipr import llm

    no_key = client.post("/api/settings/test-ai")
    assert no_key.status_code == 502 and "No API key" in no_key.json()["detail"]

    monkeypatch.setattr(llm, "check_provider", lambda settings: {"ok": True, "provider": "gemini", "model": "gemini-3.8-flash"})
    ok = client.post("/api/settings/test-ai").json()
    assert ok["ok"] is True and "Gemini (Google) answered using gemini-3.8-flash" in ok["message"]

    def rejected(settings):
        raise llm.LLMError("The Gemini API key was rejected. Check it in settings.")

    monkeypatch.setattr(llm, "check_provider", rejected)
    bad_key = client.post("/api/settings/test-ai")
    assert bad_key.status_code == 502 and "rejected" in bad_key.json()["detail"]


def test_index_and_missing_media(client):
    assert client.get("/media/clips/424242.mp4").status_code == 404
    assert client.get("/media/frames/..%2F..%2Fsnipr.jpg").status_code == 404
    font = client.get("/static/fonts/Inter-800.ttf")
    assert font.status_code == 200 and font.headers["content-type"].startswith("font/ttf")
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    script = client.get("/static/js/main.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    # The browser must ask for a newer copy every time, or an update stays invisible until a hard refresh.
    assert script.headers["cache-control"] == "no-cache" and page.headers["cache-control"] == "no-cache"
