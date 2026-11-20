// Review queue: clips grouped by streamer and stream on the left, the rendered
// clip in the middle, the decision on the right.

import { h, icon, replace, isTypingTarget } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import { toast, toastError, loadingBlock, errorBlock, emptyBlock, llmNotice, pageHead, destinationChecks, thumb, notice } from '../ui.js';
import { createPlayer, playerKey } from '../player.js';

const RENDER_POLL_MS = 3000;

// Kept in memory only, so the filter and the ticked destinations survive a
// trip to Edit clip and back. Nothing here is persisted.
export const reviewMemory = {
  streamerId: '',
  selectedId: null,
  destinations: new Map(), // clip id -> Set of destination ids the user ticked
};

export function mount(root, ctx) {
  let alive = true;
  let clips = [];
  let dests = [];
  let destsError = null;
  let streamers = [];
  let loaded = false;
  let busy = false;
  let player = null;
  // The last review_count from /api/state that this view has acted on.
  let seenCount = null;
  let undo = null; // { run, toast }
  let renderTimer = null;
  let filterKey = '';

  const fromQuery = Number(ctx.query.get('clip'));
  if (fromQuery) reviewMemory.selectedId = fromQuery;

  // ---------- Skeleton ----------
  const filter = h('select', { class: 'input', id: 'f-streamer', style: { minWidth: '200px' } }, h('option', { value: '' }, 'All streamers'));
  filter.addEventListener('change', () => {
    reviewMemory.streamerId = filter.value;
    load();
  });
  const head = pageHead({
    title: 'Review',
    sub: 'Each clip is approved or rejected on its own.',
    action: h('div', { class: 'field' }, h('label', { class: 'micro', for: 'f-streamer' }, 'Streamer'), filter),
  });
  const noticeSlot = h('div', { hidden: true });
  const body = h('div');
  root.append(head.el, noticeSlot, body);

  const queueEl = h('section', { class: 'review__queue', 'aria-label': 'Queue' });
  const previewEl = h('section', { class: 'review__preview', 'aria-label': 'Clip preview' });
  const detailsEl = h('section', { class: 'review__details', 'aria-label': 'Clip details' });
  const columns = h('div', { class: 'review' }, queueEl, previewEl, detailsEl);

  // ---------- Data ----------
  const current = () => clips.find((c) => c.id === reviewMemory.selectedId) ?? null;
  const enabledDests = () => dests.filter((d) => d.enabled !== false);

  function selectionFor(clip) {
    let set = reviewMemory.destinations.get(clip.id);
    if (!set) {
      const known = new Set(enabledDests().map((d) => d.id));
      set = new Set((clip.destination_ids ?? []).filter((id) => known.has(id)));
      // Only remember it once the destinations are known, so a failed load
      // does not freeze an empty choice.
      if (dests.length) reviewMemory.destinations.set(clip.id, set);
    }
    return set;
  }

  async function load({ silent = false } = {}) {
    if (!silent) {
      loaded = false;
      destroyPlayer();
      replace(body, loadingBlock('Loading the queue'));
    }
    const streamerId = reviewMemory.streamerId;
    try {
      const [clipList, destList, streamerList] = await Promise.all([
        api.clips.list({ status: 'pending_review', streamer_id: streamerId || undefined }),
        api.destinations.list().then((list) => ({ list }), (error) => ({ error })),
        api.streamers.list().catch(() => null),
      ]);
      if (!alive || streamerId !== reviewMemory.streamerId) return;
      const before = JSON.stringify(clips);
      clips = clipList ?? [];
      if (destList.list) { dests = destList.list; destsError = null; } else { destsError = destList.error; }
      if (streamerList) streamers = streamerList;
      const wasLoaded = loaded;
      loaded = true;
      paintFilter();
      if (!current()) reviewMemory.selectedId = clips[0]?.id ?? null;
      // A quiet reload that found nothing new leaves the page untouched.
      if (!silent || !wasLoaded || before !== JSON.stringify(clips)) renderAll();
      scheduleRenderPoll();
    } catch (err) {
      if (!alive) return;
      if (silent) return; // The nav shows the connection problem; keep what is on screen.
      replace(body, errorBlock(err.message, () => load()));
    }
  }

  function reloadDestinations({ showError = false } = {}) {
    api.destinations.list().then((list) => {
      if (!alive) return;
      dests = list;
      destsError = null;
      if (loaded && clips.length) renderDetails();
    }, (err) => {
      if (alive && showError) toastError(err);
    });
  }

  function paintFilter() {
    const options = new Map();
    for (const s of streamers) options.set(String(s.id), s.name);
    for (const c of clips) if (!options.has(String(c.streamer_id))) options.set(String(c.streamer_id), c.streamer_name);
    // Rebuild only when the list changed, so an open dropdown is not closed by a quiet reload.
    const key = JSON.stringify([...options]);
    if (key !== filterKey) {
      filterKey = key;
      replace(filter, h('option', { value: '' }, 'All streamers'),
        [...options].map(([id, name]) => h('option', { value: id }, name)));
    }
    filter.value = reviewMemory.streamerId;
    if (filter.value !== reviewMemory.streamerId) { reviewMemory.streamerId = ''; filter.value = ''; }
  }

  // ---------- Rendering ----------
  function renderAll() {
    if (!loaded) return;
    const filterName = filter.selectedOptions[0] && filter.value ? filter.selectedOptions[0].textContent : '';
    head.setSub(clips.length
      ? `${fmt.plural(clips.length, 'clip')} waiting${filterName ? ` from ${filterName}` : ''}. Each one is approved or rejected on its own.`
      : 'No clips waiting.');
    if (!clips.length) {
      destroyPlayer();
      replace(body, filterName
        ? emptyBlock(`Nothing is waiting from ${filterName}`, 'Clips from other streamers may still be in the queue.',
          h('button', { type: 'button', class: 'btn', onclick: () => { reviewMemory.streamerId = ''; filter.value = ''; load(); } }, 'Show all streamers'))
        : emptyBlock('Nothing is waiting for review', 'New clips show up here once a stream has been processed. Activity shows what Snipr is working on.',
          h('a', { class: 'btn', href: '#/activity' }, 'Open Activity'),
          h('a', { class: 'btn btn--quiet', href: '#/streamers' }, 'Streamers')));
      return;
    }
    if (columns.parentNode !== body) replace(body, columns);
    renderQueue();
    renderPreview();
    renderDetails();
  }

  function groupClips() {
    const groups = [];
    const byVod = new Map();
    for (const clip of clips) {
      let group = byVod.get(clip.vod_id);
      if (!group) {
        group = { clip, clips: [] };
        byVod.set(clip.vod_id, group);
        groups.push(group);
      }
      group.clips.push(clip);
    }
    return groups;
  }

  function renderQueue() {
    const scroll = queueEl.scrollTop;
    replace(queueEl, groupClips().map((group) => h('div', { class: 'queue-group' },
      h('div', { class: 'queue-group__head micro', title: group.clip.vod_title },
        h('span', null, `${group.clip.streamer_name} · VOD ${fmt.day(group.clip.vod_date)}`),
        h('span', null, fmt.plural(group.clips.length, 'clip'))),
      group.clips.map(queueItem))));
    queueEl.scrollTop = scroll;
  }

  function queueItem(clip) {
    const selected = clip.id === reviewMemory.selectedId;
    return h('button', {
      type: 'button', class: 'queue-item', 'aria-current': selected ? 'true' : null, dataset: { clip: String(clip.id) },
      onclick: () => select(clip.id),
    },
    thumb(clip.thumb_url),
    h('span', { class: 'queue-item__body' },
      h('span', { class: 'queue-item__headline' }, clip.headline || clip.title || 'Untitled clip'),
      h('span', { class: 'queue-item__meta' },
        h('span', null, fmt.clock(clip.duration_s)),
        h('span', null, fmt.label(fmt.MODE, clip.mode)),
        (clip.flags ?? []).map((flag) => h('span', { class: 'queue-item__flag' }, icon('warn', 12), fmt.label(fmt.FLAG_SHORT, flag))),
        clip.render_state === 'rendering' ? h('span', { class: 'queue-item__render' }, 'Rendering') : null,
        clip.render_state === 'failed' ? h('span', { class: 'queue-item__render queue-item__render--failed' }, 'Render failed') : null)),
    h('span', { class: 'queue-item__score' }, fmt.score(clip.score)));
  }

  function destroyPlayer() {
    if (player) { player.destroy(); player = null; }
  }

  function renderPreview() {
    const clip = current();
    if (!clip) { destroyPlayer(); replace(previewEl); return; }
    if (player && player.key === playerKey(clip)) return; // Same file: leave playback alone.
    destroyPlayer();
    player = createPlayer(clip);
    replace(previewEl, player.el,
      h('div', { class: 'player__meta' }, `${fmt.label(fmt.LAYOUT, clip.layout?.type ?? 'fill')} · 1080 × 1920`));
  }

  function renderDetails() {
    const clip = current();
    if (!clip) { replace(detailsEl); return; }
    const hadFocus = detailsEl.contains(document.activeElement);
    const ready = clip.render_state === 'ready';
    const selected = selectionFor(clip);

    const approveBtn = h('button', { type: 'button', class: 'btn btn--primary', disabled: busy || !ready, dataset: { action: 'approve' }, onclick: () => approve(false) },
      'Approve', h('kbd', null, 'A'));
    const postNowBtn = h('button', { type: 'button', class: 'btn', disabled: busy || !ready || selected.size === 0, onclick: () => approve(true) }, 'Approve and post now');
    const syncPostNow = () => { postNowBtn.disabled = busy || !ready || selected.size === 0; };

    const description = [clip.description, clip.credit].filter((part) => part && String(part).trim()).join('\n');

    replace(detailsEl,
      h('div', { class: 'score-row' },
        h('div', { class: 'score' },
          h('span', { class: 'score__num' }, fmt.score(clip.score)),
          h('span', { class: 'micro' }, clip.min_score != null ? `Score · bar ${fmt.score(clip.min_score)}` : 'Score')),
        h('div', { class: 'chips' },
          (clip.flags ?? []).map((flag) => h('span', { class: 'chip chip--warn' }, icon('warn', 14), fmt.label(fmt.FLAG, flag))),
          h('span', { class: 'chip' }, fmt.label(fmt.MODE, clip.mode)),
          clip.layout && clip.layout.voiceover === true ? h('span', { class: 'chip chip--accent' }, 'Voiceover') : null)),
      h('div', { class: 'block' },
        h('h2', { class: 'micro' }, 'Why it was picked'),
        h('p', { class: 'reason' }, clip.reason || 'No reason was recorded for this clip.')),
      h('div', { class: 'block' },
        h('h2', { class: 'micro' }, 'Source'),
        h('div', { class: 'source' },
          h('span', { class: 'source__who' }, `${clip.streamer_name} · ${fmt.label(fmt.PLATFORM, clip.platform)}`),
          h('span', null, clip.vod_title),
          h('span', { class: 'source__time' }, `${fmt.timecode(clip.start_s)} to ${fmt.timecode(clip.end_s)} · ${fmt.secs(clip.duration_s)}`))),
      h('div', { class: 'block', style: { gap: '10px' } },
        h('h2', { class: 'micro' }, 'Post text'),
        h('dl', { class: 'post-text' },
          h('dt', null, 'Headline'), h('dd', null, clip.headline || '–',
            clip.layout && clip.layout.show_headline === false ? h('span', { class: 'faint' }, ' · not shown on the video') : null),
          h('dt', null, 'Title'), h('dd', null, clip.title || '–'),
          h('dt', null, 'Description'), h('dd', null, description || '–'),
          h('dt', null, 'Hashtags'), h('dd', { class: 'post-text__tags' }, fmt.hashtagsText(clip.hashtags) || '–'),
          clip.narration && String(clip.narration).trim()
            ? [h('dt', null, 'Narration'), h('dd', null, String(clip.narration).trim(),
              clip.layout && clip.layout.voiceover === true ? null : h('span', { class: 'faint' }, ' · voiceover is off, so this is not read out'))]
            : null)),
      destsError
        ? h('div', { class: 'block' }, h('h2', { class: 'micro' }, 'Destinations'),
          notice('danger', `Destinations did not load: ${destsError.message} `,
            h('button', { type: 'button', class: 'btn btn--text btn--small', onclick: () => reloadDestinations({ showError: true }) }, 'Try again')))
        : destinationChecks({ dests: enabledDests(), selected, slotPrefix: 'next slot ', onChange: syncPostNow }),
      h('div', { class: 'actions' },
        !ready ? h('p', { class: 'help', role: 'status' }, clip.render_state === 'rendering'
          ? 'Approve is off while the clip renders. You can still edit or reject it.'
          : 'This clip did not render, so it cannot be approved. Edit it to render again, or reject it.') : null,
        h('div', { class: 'actions__row' },
          approveBtn, postNowBtn,
          h('a', { class: 'btn', href: `#/review/${clip.id}/edit` }, 'Edit', h('kbd', null, 'E'))),
        h('div', { class: 'actions__row actions__row--reject' },
          h('button', { type: 'button', class: 'btn btn--danger', disabled: busy, onclick: () => reject(null) }, 'Reject', h('kbd', null, 'R')),
          h('span', { class: 'actions__hint' }, 'or reject with a reason'),
          fmt.REJECT_REASONS.map(([key, text]) =>
            h('button', { type: 'button', class: 'btn btn--quiet', disabled: busy, onclick: () => reject(key) }, text))),
        h('div', { class: 'keys' },
          h('span', null, h('kbd', null, '↑'), h('kbd', null, '↓'), 'move through the queue'),
          h('span', null, h('kbd', null, 'Space'), 'play or pause'))));

    if (hadFocus) {
      const target = detailsEl.querySelector('[data-action="approve"]:not(:disabled)') ?? detailsEl.querySelector('.actions .btn:not(:disabled)');
      if (target) target.focus({ preventScroll: true });
    }
  }

  function select(id, { scroll = false } = {}) {
    if (id === reviewMemory.selectedId) return;
    reviewMemory.selectedId = id;
    history.replaceState(null, '', `#/review?clip=${id}`);
    const focusInQueue = queueEl.contains(document.activeElement);
    for (const item of queueEl.querySelectorAll('.queue-item')) {
      if (Number(item.dataset.clip) === id) {
        item.setAttribute('aria-current', 'true');
        if (scroll) item.scrollIntoView({ block: 'nearest' });
        if (focusInQueue) item.focus({ preventScroll: true });
      } else {
        item.removeAttribute('aria-current');
      }
    }
    renderPreview();
    renderDetails();
  }

  function move(step) {
    if (!clips.length) return;
    const index = clips.findIndex((c) => c.id === reviewMemory.selectedId);
    const next = clips[Math.min(clips.length - 1, Math.max(0, index + step))];
    if (next) select(next.id, { scroll: true });
  }

  // ---------- Decisions ----------
  function setBusy(value) {
    busy = value;
    if (alive && loaded && clips.length) renderDetails();
  }

  function scheduledText(posts) {
    if (!posts || !posts.length) return 'Approved. Nothing was scheduled.';
    const parts = posts.map((p) => {
      const dest = `${p.destination_name} · ${fmt.label(fmt.PLATFORM, p.platform)}`;
      if (p.status === 'due') return `${dest} (due now)`;
      if (p.status === 'posting' || p.status === 'posted') return `${dest} (posting now)`;
      const at = fmt.when(p.scheduled_at);
      return at ? `${dest}, ${at}` : dest;
    });
    return `Approved. Queued for ${parts.join(' and ')}.`;
  }

  /** Take a decided clip out of the queue, move on, and offer undo. */
  function afterDecision(clip, message) {
    const index = clips.indexOf(clip);
    clips = clips.filter((c) => c !== clip);
    reviewMemory.destinations.delete(clip.id);
    const next = clips[index] ?? clips[index - 1] ?? null;
    reviewMemory.selectedId = next ? next.id : null;
    history.replaceState(null, '', next ? `#/review?clip=${next.id}` : '#/review');
    if (seenCount != null) seenCount = Math.max(0, seenCount - 1);
    renderAll();
    ctx.refreshState();
    reloadDestinations();

    if (undo) undo.toast.dismiss();
    const entry = {
      run: () => restore(clip, index),
      toast: toast(message, {
        kind: 'ok', duration: 8000,
        action: { label: 'Undo', kbd: 'U', run: () => restore(clip, index) },
        onDismiss: () => { if (undo === entry) undo = null; },
      }),
    };
    undo = entry;
  }

  async function approve(postNow) {
    const clip = current();
    if (!clip || busy) return;
    if (clip.render_state !== 'ready') {
      toast(clip.render_state === 'rendering' ? 'This clip is still rendering. Approve it when the render is done.' : 'This clip did not render, so it cannot be approved.');
      return;
    }
    const ids = [...selectionFor(clip)];
    if (postNow && !ids.length) {
      toast('Tick at least one destination to post now.');
      return;
    }
    setBusy(true);
    try {
      const result = await api.clips.approve(clip.id, ids, postNow);
      if (!alive) return;
      busy = false;
      afterDecision(clip, scheduledText(result && result.posts));
    } catch (err) {
      if (alive) toastError(err);
    } finally {
      if (alive && busy) setBusy(false);
    }
  }

  async function reject(reason) {
    const clip = current();
    if (!clip || busy) return;
    setBusy(true);
    try {
      await api.clips.reject(clip.id, reason);
      if (!alive) return;
      busy = false;
      const text = reason ? fmt.REJECT_REASONS.find(([key]) => key === reason)[1] : '';
      afterDecision(clip, text ? `Rejected: ${text.toLowerCase()}.` : 'Rejected.');
    } catch (err) {
      if (alive) toastError(err);
    } finally {
      if (alive && busy) setBusy(false);
    }
  }

  async function restore(clip, index) {
    if (undo) { const entry = undo; undo = null; entry.toast.dismiss(); }
    try {
      const restored = await api.clips.restore(clip.id);
      if (!alive) return;
      const back = restored ?? clip;
      if (!clips.some((c) => c.id === back.id)) {
        const shown = !reviewMemory.streamerId || String(back.streamer_id) === String(reviewMemory.streamerId);
        if (shown) clips.splice(Math.min(index, clips.length), 0, back);
      }
      if (clips.some((c) => c.id === back.id)) {
        reviewMemory.selectedId = back.id;
        history.replaceState(null, '', `#/review?clip=${back.id}`);
      }
      if (seenCount != null) seenCount += 1;
      renderAll();
      scheduleRenderPoll();
      ctx.refreshState();
      reloadDestinations();
      toast('Back in the queue.');
    } catch (err) {
      if (alive) toastError(err);
    }
  }

  // ---------- Polling ----------
  function scheduleRenderPoll() {
    clearTimeout(renderTimer);
    if (!alive || !clips.some((c) => c.render_state === 'rendering')) return;
    renderTimer = setTimeout(pollRendering, RENDER_POLL_MS);
  }

  async function pollRendering() {
    const pending = clips.filter((c) => c.render_state === 'rendering');
    const results = await Promise.all(pending.map((c) => api.clips.get(c.id).catch((err) => err)));
    if (!alive) return;
    let queueChanged = false;
    results.forEach((fresh, i) => {
      const old = pending[i];
      const at = clips.indexOf(old);
      if (at < 0) return;
      if (fresh instanceof Error) {
        // A 404 means the clip is gone. Anything else: try again next round.
        if (fresh.status === 404) { clips.splice(at, 1); queueChanged = true; }
        return;
      }
      if (fresh.status && fresh.status !== 'pending_review') { clips.splice(at, 1); queueChanged = true; return; }
      if (playerKey(fresh) !== playerKey(old) || fresh.headline !== old.headline) queueChanged = true;
      clips[at] = fresh;
    });
    if (queueChanged) {
      if (!current()) reviewMemory.selectedId = clips[0]?.id ?? null;
      renderAll();
    }
    scheduleRenderPoll();
  }

  // New clips arrive while the page is open: reload quietly when the server's
  // count no longer matches what is on screen. The selection stays put.
  const unsubscribe = ctx.onState((state) => {
    if (!alive) return;
    paintNotice(state);
    if (!state || !loaded || busy) return;
    if (seenCount == null) { seenCount = state.review_count; return; }
    if (state.review_count !== seenCount) {
      seenCount = state.review_count;
      load({ silent: true });
    }
  });

  function paintNotice(state) {
    const el = llmNotice(state);
    noticeSlot.hidden = !el;
    replace(noticeSlot, el);
  }

  // ---------- Keyboard ----------
  function onKey(e) {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTypingTarget(e.target)) return;
    const tag = e.target instanceof HTMLElement ? e.target.tagName : '';
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;

    if (key === 'u' && undo && !e.repeat) { e.preventDefault(); undo.run(); return; }
    if (!loaded || !clips.length) return;

    if (key === 'ArrowDown' || key === 'ArrowUp') {
      if (tag === 'VIDEO' || (tag === 'INPUT' && e.target.type === 'range')) return;
      e.preventDefault();
      move(key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (key === ' ') {
      const onQueueItem = tag === 'BUTTON' && e.target.classList.contains('queue-item');
      if (['BUTTON', 'A', 'INPUT', 'SUMMARY', 'VIDEO'].includes(tag) && !onQueueItem) return;
      e.preventDefault();
      if (player) player.toggle();
      return;
    }
    if (e.repeat) return;
    if (key === 'a') { e.preventDefault(); approve(false); }
    else if (key === 'r') { e.preventDefault(); reject(null); }
    else if (key === 'e') {
      const clip = current();
      if (clip) { e.preventDefault(); ctx.navigate(`#/review/${clip.id}/edit`); }
    }
  }
  document.addEventListener('keydown', onKey);

  paintNotice(ctx.state());
  load();

  return () => {
    alive = false;
    clearTimeout(renderTimer);
    document.removeEventListener('keydown', onKey);
    unsubscribe();
    destroyPlayer();
    if (undo) undo.toast.dismiss();
  };
}
