// Edit clip: start and end, on-screen headline, post text, layout, voiceover,
// destinations.

import { h, replace } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import {
  toast, loadingBlock, errorBlock, pageHead, field, errorLine, layoutChoices, captionChoices, headlineToggle, voiceoverControls,
  destinationChecks, notice, withBusy,
} from '../ui.js';
import { createPlayer } from '../player.js';
import { createCropEditor } from '../crop-editor.js';
import { reviewMemory } from './review.js';

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const round1 = (n) => Math.round(n * 10) / 10;
const sameSet = (a, b) => a.size === b.size && [...a].every((x) => b.has(x));

export function mount(root, ctx) {
  const clipId = Number(ctx.params[0]);
  const backHref = `#/review?clip=${clipId}`;
  let alive = true;
  let player = null;
  let stopState = null;
  let renderTimer = null;

  const head = pageHead({ title: 'Edit clip', sub: '', back: { href: backHref, label: 'Back to queue' } });
  const body = h('div');
  root.append(head.el, body);

  async function load() {
    replace(body, loadingBlock('Loading the clip'));
    try {
      const [clip, dests, voices] = await Promise.all([
        api.clips.get(clipId),
        api.destinations.list().then((list) => ({ list }), (error) => ({ error })),
        api.voices().then((v) => v ?? {}, (error) => ({ error })),
      ]);
      if (!alive) return;
      build(clip, dests, voices);
    } catch (err) {
      if (alive) replace(body, errorBlock(err.message, load));
    }
  }

  function build(clip, destResult, voices) {
    head.setSub(`${clip.streamer_name} · ${clip.vod_title}`);
    const dests = (destResult.list ?? []).filter((d) => d.enabled !== false);
    // The streamer's length limits and the stream's length come with the clip.
    // If they are missing the server still checks on save.
    const limits = clip.clip_min_s != null && clip.clip_max_s != null && Number.isFinite(Number(clip.clip_min_s)) && Number.isFinite(Number(clip.clip_max_s))
      ? { min: Number(clip.clip_min_s), max: Number(clip.clip_max_s) } : null;
    const vodEnd = clip.vod_duration_s != null && Number(clip.vod_duration_s) > 0 ? Number(clip.vod_duration_s) : null;
    const range = clip.source_range && Number.isFinite(clip.source_range.start_s) && Number.isFinite(clip.source_range.end_s)
      ? clip.source_range : null;
    const raw = clip.layout ?? {};
    const savedLayout = {
      type: raw.type ?? 'fill', crop: raw.crop ?? null, facecam: raw.facecam ?? null, gameplay: raw.gameplay ?? null,
      caption_style: raw.caption_style ?? 'box', show_headline: raw.show_headline !== false,
      voiceover: raw.voiceover === true, voice: raw.voice ?? null, mix: raw.mix === 'mute' ? 'mute' : 'duck',
    };
    const savedNarration = String(clip.narration ?? '').trim();
    const wordsPerSecond = voices && Number(voices.words_per_second) > 0 ? Number(voices.words_per_second) : null;
    const frame = clip.sample_frame ?? null;

    // ----- Editable state -----
    const m = {
      start: Number(clip.start_s),
      end: Number(clip.end_s),
      layout: savedLayout.type,
      captionStyle: savedLayout.caption_style,
      showHeadline: savedLayout.show_headline,
      voice: { on: savedLayout.voiceover, voice: savedLayout.voice, mix: savedLayout.mix },
      rects: { crop: savedLayout.crop, facecam: savedLayout.facecam, gameplay: savedLayout.gameplay },
      dests: new Set((clip.destination_ids ?? []).filter((id) => dests.some((d) => d.id === id))),
    };
    const savedDests = new Set(m.dests);
    let view = { lo: 0, hi: 1 };
    let playAt = null;    // where the preview is, in stream time
    let startText = true; // false while the Start / End field holds text that cannot be read
    let endText = true;

    // ----- Preview -----
    const preview = h('section', { class: 'edit__preview', 'aria-label': 'Clip preview' });
    function showPreview(current) {
      if (player) player.destroy();
      player = createPlayer(current);
      replace(preview, player.el,
        h('p', { class: 'player__note' }, current.render_state === 'rendering'
          ? 'This clip is rendering. The preview appears when it is done.'
          : 'The preview shows the last render. It updates after you save.'));
      // The white line on the bar follows the preview, mapped onto stream time.
      if (current.render_state === 'ready') {
        player.onTime((t) => {
          playAt = Number(clip.start_s) + t;
          playhead.hidden = false;
          playhead.style.left = pct(playAt);
        });
      }
    }
    // A clip that is still rendering is checked every 3 s; the form is left alone.
    function watchRender() {
      renderTimer = setTimeout(async () => {
        try {
          const fresh = await api.clips.get(clip.id);
          if (!alive) return;
          if (fresh.render_state !== 'rendering') { showPreview(fresh); return; }
        } catch { /* The nav shows a lost connection; try again next round. */ }
        if (alive) watchRender();
      }, 3000);
    }

    // ----- Start and end -----
    const gapBefore = h('div', { class: 'timeline__gap', hidden: true });
    const gapAfter = h('div', { class: 'timeline__gap', hidden: true });
    const rangeEl = h('div', { class: 'timeline__range' });
    const startHandle = h('button', { type: 'button', class: 'timeline__handle timeline__handle--start', role: 'slider', 'aria-label': 'Start of the clip. Drag, or use the arrow keys.' });
    const endHandle = h('button', { type: 'button', class: 'timeline__handle timeline__handle--end', role: 'slider', 'aria-label': 'End of the clip. Drag, or use the arrow keys.' });
    const playhead = h('div', { class: 'timeline__playhead', 'aria-hidden': 'true', hidden: true });
    const timeline = h('div', { class: 'timeline' }, gapBefore, gapAfter, rangeEl, playhead, startHandle, endHandle);
    const ticks = h('div', { class: 'timeline__ticks', 'aria-hidden': 'true' });

    const startInput = h('input', { type: 'text', class: 'input input--mono input--w132', id: 'e-start', autocomplete: 'off', spellcheck: 'false' });
    const endInput = h('input', { type: 'text', class: 'input input--mono input--w132', id: 'e-end', autocomplete: 'off', spellcheck: 'false' });
    const lengthEl = h('div', { class: 'time-len', role: 'status' });
    const timeError = errorLine();
    const rangeNote = h('div', { hidden: true });

    const nudge = (which, delta, text, labelText) => h('button', {
      type: 'button', class: 'btn', 'aria-label': labelText,
      onclick: () => { setTime(which, m[which] + delta); },
    }, text);

    function setView() {
      const lo = Math.min(m.start, clip.start_s, range ? range.start_s : Infinity);
      const hi = Math.max(m.end, clip.end_s, range ? range.end_s : -Infinity);
      const padding = Math.max(10, (hi - lo) * 0.15);
      view = { lo: Math.max(0, lo - padding), hi: vodEnd != null ? Math.max(hi, Math.min(vodEnd, hi + padding)) : hi + padding };
    }
    const pct = (t) => `${clamp(((t - view.lo) / (view.hi - view.lo)) * 100, 0, 100)}%`;

    function paintTimeline() {
      rangeEl.style.left = pct(m.start);
      rangeEl.style.width = `${Math.max(0, parseFloat(pct(m.end)) - parseFloat(pct(m.start)))}%`;
      startHandle.style.left = pct(m.start);
      endHandle.style.left = pct(m.end);
      for (const [handle, value] of [[startHandle, m.start], [endHandle, m.end]]) {
        handle.setAttribute('aria-valuemin', String(Math.round(view.lo)));
        handle.setAttribute('aria-valuemax', String(Math.round(view.hi)));
        handle.setAttribute('aria-valuenow', String(value));
        handle.setAttribute('aria-valuetext', fmt.timecode(value));
      }
      if (range) {
        gapBefore.hidden = range.start_s <= view.lo;
        gapBefore.style.left = '0';
        gapBefore.style.width = pct(range.start_s);
        gapAfter.hidden = range.end_s >= view.hi;
        gapAfter.style.left = pct(range.end_s);
        gapAfter.style.right = '0';
      }
      playhead.hidden = playAt == null;
      if (playAt != null) playhead.style.left = pct(playAt);
      const span = view.hi - view.lo;
      replace(ticks, [0, 1, 2, 3].map((i) => h('span', null, fmt.timecode(Math.round(view.lo + (span * i) / 3)))));
    }

    /** Set start or end from a nudge, a drag or a committed field. */
    function setTime(which, value, { fromDrag = false } = {}) {
      m[which] = Math.max(0, round1(vodEnd != null ? Math.min(value, vodEnd) : value));
      if (which === 'start') { startText = true; startInput.value = fmt.timecode(m.start); }
      else { endText = true; endInput.value = fmt.timecode(m.end); }
      if (!fromDrag) setView();
      paintTimeline();
      validate();
    }

    function bindHandle(handle, which) {
      handle.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        handle.focus({ preventScroll: true });
        const bounds = timeline.getBoundingClientRect();
        const onMove = (ev) => {
          const ratio = clamp((ev.clientX - bounds.left) / bounds.width, 0, 1);
          let t = view.lo + ratio * (view.hi - view.lo);
          t = which === 'start' ? Math.min(t, m.end - 0.1) : Math.max(t, m.start + 0.1);
          setTime(which, t, { fromDrag: true });
        };
        const onUp = (ev) => {
          handle.releasePointerCapture(ev.pointerId);
          handle.removeEventListener('pointermove', onMove);
          handle.removeEventListener('pointerup', onUp);
          handle.removeEventListener('pointercancel', onUp);
          setView();
          paintTimeline();
        };
        handle.setPointerCapture(e.pointerId);
        handle.addEventListener('pointermove', onMove);
        handle.addEventListener('pointerup', onUp);
        handle.addEventListener('pointercancel', onUp);
      });
      handle.addEventListener('keydown', (e) => {
        const dir = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
        if (!dir) return;
        e.preventDefault();
        setTime(which, m[which] + dir * (e.shiftKey ? 0.1 : 1));
      });
    }
    bindHandle(startHandle, 'start');
    bindHandle(endHandle, 'end');

    function bindInput(input, which) {
      // While typing: follow the value, but keep the bar's scale still.
      input.addEventListener('input', () => {
        const value = fmt.parseTimecode(input.value);
        if (which === 'start') startText = value != null; else endText = value != null;
        if (value != null) { m[which] = value; paintTimeline(); }
        validate();
      });
      const commit = () => {
        const value = fmt.parseTimecode(input.value);
        if (value != null) setTime(which, value);
      };
      input.addEventListener('change', commit);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); return; }
        if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
        e.preventDefault();
        setTime(which, m[which] + (e.key === 'ArrowUp' ? 1 : -1) * (e.shiftKey ? 0.1 : 1));
      });
    }
    bindInput(startInput, 'start');
    bindInput(endInput, 'end');

    showPreview(clip);
    if (clip.render_state === 'rendering') watchRender();

    const timeSection = h('section', { class: 'section' },
      h('h2', { class: 'micro' }, 'Start and end'),
      timeline, ticks,
      h('div', { class: 'time-fields' },
        h('div', { class: 'field' }, h('label', { class: 'label', for: 'e-start' }, 'Start'),
          h('div', { class: 'time-field__row' }, startInput,
            nudge('start', -1, '−1 s', 'Start one second earlier'), nudge('start', 1, '+1 s', 'Start one second later'))),
        h('div', { class: 'field' }, h('label', { class: 'label', for: 'e-end' }, 'End'),
          h('div', { class: 'time-field__row' }, endInput,
            nudge('end', -1, '−1 s', 'End one second earlier'), nudge('end', 1, '+1 s', 'End one second later'))),
        lengthEl),
      timeError,
      rangeNote,
      h('p', { class: 'help', style: { fontSize: '13px', maxWidth: '64ch' } },
        'The source VOD is kept until every clip from it is reviewed, so you can extend past the original cut.',
        range ? ' The striped parts of the bar are not downloaded yet.' : ''));

    // ----- Text -----
    const headline = h('input', { type: 'text', class: 'input', value: clip.headline ?? '' });
    const headlineHelp = h('span', { class: 'help' });
    const title = h('input', { type: 'text', class: 'input', value: clip.title ?? '' });
    const description = h('textarea', { class: 'input', rows: '4' });
    description.value = clip.description ?? '';
    const credit = h('input', { type: 'text', class: 'input', value: clip.credit ?? '' });
    const tags = h('input', { type: 'text', class: 'input input--tags', value: fmt.hashtagsText(clip.hashtags), autocomplete: 'off', spellcheck: 'false' });

    function paintHeadlineHelp() {
      const words = headline.value.trim().split(/\s+/).filter(Boolean).length;
      headlineOff.hidden = m.showHeadline;
      headlineHelp.textContent = m.showHeadline
        ? `${fmt.plural(words, 'word')}. On screen for the whole clip, so keep it to about six.`
        : 'Show headline is off for this clip, so this text is kept but not put on the video.';
      headlineHelp.classList.toggle('help--warn', m.showHeadline && words > 8);
    }

    const headlineField = field({ label: 'Headline', control: headline, help: headlineHelp });
    const headlineOff = h('span', { class: 'label__note', hidden: true }, ' · not shown on the video');
    headlineField.querySelector('label').append(headlineOff);

    const textSection = h('section', { class: 'section section--fields' },
      h('h2', { class: 'micro' }, 'Text'),
      headlineField,
      field({ label: 'Title', control: title }),
      field({ label: 'Description', control: description }),
      field({ label: 'Credit', control: credit, help: 'Added on its own line under the description when the clip is posted. You can change it here.' }),
      field({ label: 'Hashtags', control: tags, help: 'Separate with spaces. The # is optional.' }));

    // ----- Layout, crop, destinations -----
    let cropEditor = null;
    const layoutSet = layoutChoices({
      value: m.layout,
      onChange: (next) => {
        m.layout = next;
        if (cropEditor) cropEditor.setLayout(next);
        validate();
      },
    });
    const captionSet = captionChoices({ value: m.captionStyle, onChange: (next) => { m.captionStyle = next; validate(); } });
    const headlineSwitch = headlineToggle({ value: m.showHeadline, onChange: (next) => { m.showHeadline = next; paintHeadlineHelp(); validate(); } });
    const renderNote = h('p', { class: 'help' }, 'Changing the layout, the caption style or the headline switch re-renders the clip.');
    let cropBlock = null;
    if (frame) {
      cropEditor = createCropEditor({ frame, layout: m.layout, rects: m.rects, onChange: (rects) => { m.rects = rects; validate(); } });
      cropBlock = h('details', { class: 'details' },
        h('summary', null, 'Adjust the crop for this clip'),
        h('div', { class: 'details__body' },
          cropEditor.el,
          h('div', { class: 'crop-tools' },
            h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => cropEditor.reset() }, 'Reset to centre crop'),
            h('span', { class: 'help' }, 'Drag a rectangle to move it, or a corner to resize it. Arrow keys nudge a focused rectangle.'))));
    }

    // ----- Voiceover -----
    const narration = h('textarea', { class: 'input', rows: '5' });
    narration.value = clip.narration ?? '';
    const countEl = h('span', { class: 'count', role: 'status' });
    const narrationError = errorLine();
    const writeBtn = h('button', { type: 'button', class: 'btn' }, 'Write narration');
    const rewriteBtn = h('button', { type: 'button', class: 'btn' }, 'Rewrite mine');
    const writeHint = h('span', { class: 'help' });
    const writeStatus = h('div', { hidden: true, 'aria-live': 'polite' });
    let serverBudget = null; // from the last write-narration answer, used if /api/voices did not load

    // How to narrate: the reviewer's own directions, reusable as named templates.
    const style = h('textarea', { class: 'input', rows: '3', maxlength: '1500',
      placeholder: 'For example: hype sports commentator, short punchy sentences, end on a question for the viewer.' });
    const templateSelect = h('select', { class: 'input' });
    const templateName = h('input', { type: 'text', class: 'input', maxlength: '60', placeholder: 'Template name' });
    const templateSave = h('button', { type: 'button', class: 'btn btn--small' }, 'Save as template');
    const templateDelete = h('button', { type: 'button', class: 'btn btn--text btn--small' }, 'Delete template');
    const templateNote = h('span', { class: 'help', role: 'status' });
    let templates = [];
    function paintTemplates(selectedId = '') {
      replace(templateSelect,
        h('option', { value: '' }, templates.length ? 'Choose a saved template' : 'No saved templates yet'),
        templates.map((t) => h('option', { value: String(t.id) }, t.name)));
      templateSelect.value = selectedId;
      templateDelete.hidden = !templateSelect.value;
    }
    api.templates.list().then((list) => { if (alive) { templates = list ?? []; paintTemplates(); } },
      (err) => { if (alive) templateNote.textContent = `Templates did not load: ${err.message}`; });
    templateSelect.addEventListener('change', () => {
      const chosen = templates.find((t) => String(t.id) === templateSelect.value);
      if (chosen) { style.value = chosen.prompt; templateName.value = chosen.name; }
      templateDelete.hidden = !templateSelect.value;
      templateNote.textContent = '';
    });
    templateSave.addEventListener('click', async () => {
      templateNote.textContent = '';
      const name = templateName.value.trim();
      if (!style.value.trim()) { templateNote.textContent = 'Write the instructions first.'; style.focus(); return; }
      if (!name) { templateNote.textContent = 'Give the template a name.'; templateName.focus(); return; }
      try {
        const saved = await withBusy(templateSave, () => api.templates.save(name, style.value.trim()));
        if (!alive || !saved) return;
        templates = [...templates.filter((t) => t.id !== saved.id), saved].sort((a, b) => a.name.localeCompare(b.name));
        paintTemplates(String(saved.id));
        templateNote.textContent = `Saved as "${saved.name}". Saving again under the same name updates it.`;
      } catch (err) {
        if (alive) templateNote.textContent = err.message;
      }
    });
    templateDelete.addEventListener('click', async () => {
      const id = Number(templateSelect.value);
      if (!id) return;
      try {
        await withBusy(templateDelete, () => api.templates.remove(id));
        if (!alive) return;
        templates = templates.filter((t) => t.id !== id);
        paintTemplates();
        templateNote.textContent = 'Template deleted. The instructions above are left as they are.';
      } catch (err) {
        if (alive) templateNote.textContent = err.message;
      }
    });

    const voiceCtl = voiceoverControls({ value: m.voice, voices, onChange: (next) => { m.voice = next; validate(); } });
    const voiceBody = h('div', { class: 'voice-grid' },
      h('div', { class: 'block' },
        field({ label: 'Narration', control: narration, help: countEl }),
        narrationError,
        field({ label: 'How to narrate', control: style, help: 'Optional. Your directions for tone and style. Used by Write narration and Rewrite mine.' }),
        h('div', { class: 'row row--tight' }, templateSelect, templateDelete),
        h('div', { class: 'row row--tight' }, templateName, templateSave),
        templateNote,
        h('div', { class: 'row row--tight' }, writeBtn, rewriteBtn),
        writeHint,
        writeStatus),
      voiceCtl.optionsEl);
    const voiceSection = h('section', { class: 'section' },
      h('h2', { class: 'micro' }, 'Voiceover'),
      voiceCtl.toggleEl,
      voiceBody,
      h('p', { class: 'help' }, 'Turning voiceover on or off, changing the voice or the stream sound, or editing the narration while it is on re-renders the clip.'));

    function wordBudget() {
      if (wordsPerSecond != null) return Math.max(0, Math.floor(wordsPerSecond * (m.end - m.start - 1)));
      return serverBudget;
    }
    function paintVoice() {
      voiceBody.hidden = !m.voice.on;
      const words = fmt.wordCount(narration.value);
      const budget = wordBudget();
      const over = budget != null && words > budget;
      countEl.textContent = budget == null ? fmt.plural(words, 'word')
        : over ? `${words} of ${budget} words. ${fmt.plural(words - budget, 'word')} too many for this clip: shorten it or lengthen the clip.`
          : `${words} of ${budget} words fit this clip.`;
      countEl.classList.toggle('count--over', over);
      const state = ctx.state();
      const moved = round1(m.start) !== round1(Number(clip.start_s)) || round1(m.end) !== round1(Number(clip.end_s));
      writeHint.textContent = state && state.llm_ready === false
        ? 'Writing one automatically needs an API key. You can still type a narration by hand.'
        : moved ? 'The draft is written for the saved start and end. Save first if you want it to match the new cut.'
          : 'Write narration drafts a new script. Rewrite mine reworks what you typed. Nothing is saved until you save.';
      rewriteBtn.disabled = !narration.value.trim();
    }
    stopState = ctx.onState(() => { if (alive) paintVoice(); });

    async function draft(button, busyText, doneText, call) {
      const previous = narration.value;
      writeStatus.hidden = false;
      replace(writeStatus, h('div', { class: 'notice', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        h('span', null, busyText)));
      try {
        const result = await withBusy([writeBtn, rewriteBtn], call);
        if (!alive) return;
        narration.value = (result && result.narration) || '';
        if (result && result.word_budget != null && Number.isFinite(Number(result.word_budget))) serverBudget = Number(result.word_budget);
        const putBack = previous.trim() && previous !== narration.value
          ? h('button', { type: 'button', class: 'btn btn--text btn--small', onclick: () => { narration.value = previous; writeStatus.hidden = true; validate(); narration.focus(); } }, 'Put back what was there')
          : null;
        replace(writeStatus, h('div', { class: 'notice' }, h('span', null, doneText, putBack)));
        narration.focus();
      } catch (err) {
        if (!alive) return;
        replace(writeStatus, notice(err.status === 409 ? 'warn' : 'danger', `${err.message} You can still type a narration by hand.`));
      }
      validate();
    }
    writeBtn.addEventListener('click', () => draft(writeBtn,
      'Writing a narration for this clip. This takes 5 to 20 seconds.',
      'Draft written. Edit it if you like, then save. ',
      () => api.clips.writeNarration(clip.id, style.value.trim())));
    rewriteBtn.addEventListener('click', () => draft(rewriteBtn,
      'Rewriting your narration. This takes a few seconds.',
      'Rewritten. Edit it if you like, then save. ',
      () => api.clips.rewriteNarration(clip.id, { text: narration.value, instructions: style.value.trim(), start_s: m.start, end_s: m.end })));
    narration.addEventListener('input', () => validate());

    const destSet = destResult.error
      ? h('div', { class: 'block' }, h('h2', { class: 'micro' }, 'Destinations'),
        notice('danger', `Destinations did not load: ${destResult.error.message} They stay as they are when you save.`))
      : destinationChecks({ dests, selected: m.dests, onChange: () => validate() });

    // ----- Footer -----
    const saveBtn = h('button', { type: 'submit', class: 'btn btn--primary' }, 'Save changes');
    const footNote = h('span', { class: 'form-foot__note', role: 'status' });
    const saveError = h('div', { hidden: true });

    const form = h('form', { class: 'edit__form', novalidate: true },
      timeSection,
      h('div', { class: 'edit__cols' },
        textSection,
        h('div', { class: 'edit__side' },
          h('div', { class: 'block', style: { gap: '16px' } }, layoutSet, cropBlock, captionSet, headlineSwitch, renderNote),
          destSet)),
      voiceSection,
      saveError,
      h('div', { class: 'form-foot' },
        saveBtn,
        h('a', { class: 'btn', href: backHref }, 'Cancel'),
        footNote));

    // ----- Change tracking and validation -----
    function changes() {
      const body = {};
      if (round1(m.start) !== round1(Number(clip.start_s))) body.start_s = m.start;
      if (round1(m.end) !== round1(Number(clip.end_s))) body.end_s = m.end;
      if (headline.value.trim() !== (clip.headline ?? '')) body.headline = headline.value.trim();
      if (title.value.trim() !== (clip.title ?? '')) body.title = title.value.trim();
      if (description.value !== (clip.description ?? '')) body.description = description.value;
      if (credit.value.trim() !== (clip.credit ?? '')) body.credit = credit.value.trim();
      const newTags = fmt.parseHashtags(tags.value);
      if (JSON.stringify(newTags) !== JSON.stringify(clip.hashtags ?? [])) body.hashtags = newTags;
      if (narration.value.trim() !== savedNarration) body.narration = narration.value.trim();
      // The layout is always sent whole.
      const layout = {
        type: m.layout, crop: m.rects.crop ?? null, facecam: m.rects.facecam ?? null, gameplay: m.rects.gameplay ?? null,
        caption_style: m.captionStyle, show_headline: m.showHeadline,
        voiceover: m.voice.on, voice: m.voice.voice, mix: m.voice.mix,
      };
      if (JSON.stringify(layout) !== JSON.stringify(savedLayout)) body.layout = layout;
      if (!destResult.error && !sameSet(m.dests, savedDests)) body.destination_ids = [...m.dests];
      return body;
    }

    function validate() {
      const length = round1(m.end - m.start);
      let problem = '';
      if (!startText || !endText) problem = 'Write times as HH:MM:SS, for example 02:14:08.';
      else if (length <= 0) problem = 'The end has to come after the start.';
      else if (vodEnd != null && m.end > vodEnd + 0.05) problem = `The stream ends at ${fmt.timecode(vodEnd)}. The clip cannot go past that.`;
      else if (limits && length < limits.min) problem = `This is shorter than the ${fmt.secs(limits.min)} minimum for ${clip.streamer_name}.`;
      else if (limits && length > limits.max) problem = `This is longer than the ${fmt.secs(limits.max)} maximum for ${clip.streamer_name}.`;

      lengthEl.textContent = `${fmt.secs(Math.max(0, length))}${limits ? ` · limit ${limits.min} to ${limits.max} s` : ''}`;
      lengthEl.classList.toggle('time-len--bad', Boolean(problem) && startText && endText);
      startInput.classList.toggle('input--error', !startText);
      endInput.classList.toggle('input--error', !endText);
      timeError.set(problem);

      // Voiceover with nothing to read is refused by the server, so stop it here.
      const noScript = m.voice.on && !narration.value.trim();
      narrationError.set(noScript ? 'Voiceover is on, so the clip needs a narration. Write one, or turn voiceover off.' : '');
      paintVoice();

      const body = changes();
      const outside = [];
      if (range && startText && endText) {
        if (m.start < range.start_s - 0.05) outside.push('start');
        if (m.end > range.end_s + 0.05) outside.push('end');
      }
      const moved = 'start_s' in body || 'end_s' in body;
      rangeNote.hidden = !(outside.length && moved);
      if (!rangeNote.hidden) {
        replace(rangeNote, notice('warn',
          `The new ${outside.join(' and ')} ${outside.length > 1 ? 'are' : 'is'} outside the footage Snipr has for this clip (${fmt.timecode(range.start_s)} to ${fmt.timecode(range.end_s)}). `
          + 'It downloads more of the stream before rendering, so this save takes longer.'));
      }

      const rerender = moved || 'headline' in body || 'layout' in body || ('narration' in body && m.voice.on);
      const dirty = Object.keys(body).length > 0;
      if (!dirty) footNote.textContent = 'Changing the start, end, headline, layout, caption style or voiceover re-renders the clip. Other text and destinations save straight away.';
      else if (rerender) footNote.textContent = 'Saving re-renders the clip. Approve stays off in the queue until the new render is done.';
      else footNote.textContent = 'These changes save straight away. The clip is not rendered again.';
      footNote.classList.toggle('help--warn', dirty && rerender);
      saveBtn.disabled = Boolean(problem) || noScript;
      return { problem: problem || (noScript ? 'narration' : ''), body, rerender };
    }

    for (const input of [headline, title, description, credit, tags]) input.addEventListener('input', () => { paintHeadlineHelp(); validate(); });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const { problem, body, rerender } = validate();
      if (problem) return;
      if (!Object.keys(body).length) { ctx.navigate(backHref); return; }
      saveError.hidden = true;
      try {
        const updated = await withBusy(saveBtn, () => api.clips.update(clip.id, body));
        if (!alive) return;
        reviewMemory.destinations.delete(clip.id);
        const rendering = updated ? updated.render_state === 'rendering' : rerender;
        toast(rendering ? 'Saved. The clip is rendering again.' : 'Saved.', { kind: 'ok' });
        ctx.navigate(backHref);
      } catch (err) {
        if (!alive) return;
        saveError.hidden = false;
        replace(saveError, h('div', { role: 'alert' }, notice('danger', err.message)));
      }
    });

    replace(body, h('div', { class: 'edit' }, preview, form));
    startInput.value = fmt.timecode(m.start);
    endInput.value = fmt.timecode(m.end);
    setView();
    paintTimeline();
    paintHeadlineHelp();
    validate();
  }

  load();

  return () => {
    alive = false;
    if (player) player.destroy();
    if (stopState) stopState();
    clearTimeout(renderTimer);
  };
}
