// Add / edit streamer: channel link, selection mode, limits, layout and the
// crop rectangles marked on a sample frame.

import { h, replace, uid } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import {
  loadingBlock, errorBlock, pageHead, field, errorLine, layoutChoices, captionChoices, headlineToggle, voiceoverControls,
  toggle, notice, withBusy, toast,
} from '../ui.js';
import { centreRect, createCropEditor, renderResult } from '../crop-editor.js';

const RESOLVE_DELAY_MS = 500;

export function mount(root, ctx) {
  const id = ctx.params[0] ? Number(ctx.params[0]) : null;
  const editing = id != null;
  let alive = true;
  let resolveTimer = null;
  let resolveAbort = null;
  let stopState = null;

  const head = pageHead({
    title: editing ? 'Edit streamer' : 'Add streamer',
    sub: editing ? '' : 'Paste a channel link. Snipr starts watching for finished VODs as soon as you save.',
    back: { href: '#/streamers', label: 'Back to streamers' },
  });
  const body = h('div');
  root.append(head.el, body);

  async function load() {
    replace(body, loadingBlock(editing ? 'Loading the streamer' : 'Loading'));
    try {
      const [streamers, dests, voices] = await Promise.all([
        editing ? api.streamers.list() : api.streamers.list().catch(() => []),
        api.destinations.list().then((list) => ({ list }), (error) => ({ error })),
        api.voices().then((v) => v ?? {}, (error) => ({ error })),
      ]);
      if (!alive) return;
      let streamer = null;
      if (editing) {
        streamer = (streamers ?? []).find((s) => s.id === id);
        if (!streamer) {
          replace(body, errorBlock('This streamer does not exist any more.', null),
            h('p', null, h('a', { href: '#/streamers' }, 'Back to streamers')));
          return;
        }
      }
      build(streamer, streamers ?? [], dests, voices);
    } catch (err) {
      if (alive) replace(body, errorBlock(err.message, load));
    }
  }

  function build(streamer, allStreamers, destResult, voices) {
    const dests = destResult.list ?? [];
    if (editing) head.setSub(`${streamer.name} · ${fmt.channelShort(streamer.channel_url)}`);

    // ----- State that is not held by a form control -----
    let layout = streamer ? streamer.layout : 'fill';
    let rects = {
      crop: streamer ? streamer.crop_rect : null,
      facecam: streamer ? streamer.facecam_rect : null,
      gameplay: streamer ? streamer.gameplay_rect : null,
    };
    let captionStyle = streamer && streamer.caption_style ? streamer.caption_style : 'box';
    let showHeadline = streamer ? streamer.show_headline !== false : true;
    let voice = {
      on: Boolean(streamer && streamer.voiceover),
      voice: streamer && streamer.voice ? streamer.voice : null,
      mix: streamer && streamer.voiceover_mix === 'mute' ? 'mute' : 'duck',
    };
    let frame = streamer ? streamer.sample_frame ?? null : null;
    let frameToken = null;
    let resolved = null;     // last answer from /resolve
    let resolvedFor = '';    // the link text it belongs to
    let nameTouched = editing;
    let savedSnapshot = '';

    // ----- Channel link -----
    const link = h('input', { type: 'url', class: 'input', id: 's-link', value: streamer ? streamer.channel_url : '', placeholder: 'https://twitch.tv/name', autocomplete: 'off', spellcheck: 'false' });
    const linkStatus = h('div', { 'aria-live': 'polite' });
    const linkError = errorLine();

    const platform = h('select', { class: 'input', id: 's-platform' },
      h('option', { value: 'twitch' }, 'Twitch'),
      h('option', { value: 'youtube' }, 'YouTube'),
      h('option', { value: 'kick', disabled: true }, 'Kick (ingest not built yet)'));
    platform.value = streamer ? streamer.platform : 'twitch';

    const name = h('input', { type: 'text', class: 'input', id: 's-name', value: streamer ? streamer.name : '', autocomplete: 'off' });
    const nameError = errorLine();
    name.addEventListener('input', () => { nameTouched = true; nameError.set(''); syncDirty(); });

    function paintResolve(kind, payload) {
      link.classList.remove('input--warn', 'input--error');
      if (kind === 'loading') { replace(linkStatus, h('span', { class: 'help' }, 'Checking the link')); return; }
      if (kind === 'error') { link.classList.add('input--error'); replace(linkStatus, h('span', { class: 'help help--error' }, payload)); return; }
      if (kind !== 'ok') { replace(linkStatus); return; }
      const out = [];
      if (payload.supported === false) {
        link.classList.add('input--warn');
        out.push(notice('warn', payload.message || `${fmt.label(fmt.PLATFORM, payload.platform)} is not supported yet.`));
      }
      const dup = payload.duplicate_of;
      if (dup && dup.id !== id) {
        const other = allStreamers.find((s) => s.id === dup.id);
        link.classList.add('input--warn');
        out.push(notice('warn', `This looks like ${dup.name}, who you already watch${other ? ` on ${fmt.label(fmt.PLATFORM, other.platform)}` : ''}. Adding the same person twice produces duplicate clips.`));
      }
      replace(linkStatus, out);
    }

    async function resolveLink() {
      const text = link.value.trim();
      if (resolveAbort) resolveAbort.abort();
      resolved = null;
      resolvedFor = '';
      if (!text) { paintResolve('idle'); syncSubmit(); return; }
      resolveAbort = new AbortController();
      const mine = resolveAbort;
      paintResolve('loading');
      try {
        const result = await api.streamers.resolve(text, mine.signal);
        if (!alive || mine !== resolveAbort) return;
        resolved = result;
        resolvedFor = text;
        if (result.platform) platform.value = result.platform;
        if (result.name && (!nameTouched || !name.value.trim())) { name.value = result.name; nameError.set(''); }
        paintResolve('ok', result);
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        if (!alive || mine !== resolveAbort) return;
        paintResolve('error', err.message);
      }
      syncSubmit();
      syncDirty();
    }

    link.addEventListener('input', () => {
      linkError.set('');
      clearTimeout(resolveTimer);
      resolveTimer = setTimeout(resolveLink, RESOLVE_DELAY_MS);
      syncDirty();
    });

    // ----- Mode and instructions -----
    const modeName = uid('mode');
    const modeInputs = {};
    const modeCard = (key) => {
      modeInputs[key] = h('input', { type: 'radio', name: modeName, value: key, checked: (streamer ? streamer.mode : 'highlights') === key, onchange: () => { paintMode(); syncDirty(); } });
      return h('label', { class: 'choice' }, modeInputs[key], fmt.MODE[key]);
    };
    const modeSet = h('fieldset', { class: 'fieldset' },
      h('legend', { class: 'label' }, 'Mode'),
      h('div', { class: 'choices choices--3' }, modeCard('highlights'), modeCard('informative'), modeCard('custom')));
    const mode = () => Object.keys(modeInputs).find((k) => modeInputs[k].checked) ?? 'highlights';

    const instructions = h('textarea', { class: 'input', id: 's-instr', rows: '3' });
    instructions.value = streamer ? streamer.instructions ?? '' : '';
    const instrNote = h('span', { class: 'label__note' });
    const instrHelp = h('span', { class: 'help' });
    const instrError = errorLine();
    instructions.addEventListener('input', () => { instrError.set(''); syncDirty(); });

    function paintMode() {
      const current = mode();
      instructions.disabled = current === 'highlights';
      instrNote.textContent = current === 'custom' ? ' · required for Custom' : current === 'informative' ? ' · optional' : ' · not used by Highlights';
      instrNote.className = current === 'custom' ? 'label__note' : 'label__opt';
      instructions.placeholder = current === 'custom' ? 'Moments where he reacts to a jump scare' : '';
      instrHelp.textContent = current === 'highlights'
        ? 'Highlights picks moments from chat and audio spikes, so there is nothing to describe.'
        : current === 'informative'
          ? 'Informative looks for self-contained explanations. Add a note here to steer it.'
          : 'Describe in your own words what Snipr should look for.';
      if (current !== 'custom') instrError.set('');
    }

    // ----- Test on last VOD -----
    const testBtn = h('button', { type: 'button', class: `btn${editing ? '' : ' btn--dashed'}`, disabled: !editing }, 'Test on last VOD');
    const testNote = h('span', { class: 'help' }, editing
      ? 'Reruns selection on the last analysed stream. Nothing is downloaded or transcribed again.'
      : 'Available once the first VOD has been processed.');
    const testOut = h('div', { hidden: true, 'aria-live': 'polite' });

    async function runTest() {
      if (isDirty()) {
        const ok = await save({ stay: true });
        if (!ok) return;
      }
      testOut.hidden = false;
      replace(testOut, h('div', { class: 'notice', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
        h('span', null, 'Running selection on the last stream. This can take up to a minute.')));
      try {
        const result = await withBusy(testBtn, () => api.streamers.testSelection(id));
        if (!alive) return;
        const picks = (result && result.picks) || [];
        replace(testOut,
          h('div', { class: 'block' },
            h('span', { class: 'micro' }, `Test on ${result && result.vod_title ? result.vod_title : 'the last stream'}`),
            result && result.message ? h('p', { class: 'muted' }, result.message) : null,
            picks.length
              ? h('div', { class: 'picks' }, picks.map((p) => h('div', { class: 'pick' },
                h('div', { class: 'pick__top' },
                  h('span', { class: 'pick__title' }, p.title || 'Untitled'),
                  h('span', { class: 'pick__score' }, fmt.score(p.score))),
                h('span', { class: 'pick__meta' }, `${fmt.timecode(p.start_s)} to ${fmt.timecode(p.end_s)} · ${fmt.secs(p.end_s - p.start_s)}`),
                p.reason ? h('span', { class: 'pick__reason' }, p.reason) : null,
                p.flags && p.flags.length ? h('span', { class: 'pick__flags' }, p.flags.map((f) => h('span', null, fmt.label(fmt.FLAG, f)))) : null)))
              : h('p', { class: 'muted' }, 'Nothing was picked with these settings.')));
      } catch (err) {
        if (!alive) return;
        replace(testOut, notice(err.status === 409 ? 'warn' : 'danger', err.message));
      }
    }
    testBtn.addEventListener('click', runTest);

    // ----- Games, limits -----
    const games = h('input', { type: 'text', class: 'input', id: 's-games', value: streamer ? streamer.games ?? '' : '', placeholder: 'GTA 6, Minecraft', autocomplete: 'off' });
    const maxClips = h('input', { type: 'number', class: 'input input--mono input--w120', id: 's-max', min: '1', step: '1', value: String(streamer ? streamer.max_clips_per_vod : 5) });
    const minLen = h('input', { type: 'number', class: 'input input--mono input--w88', min: '1', step: '1', value: String(streamer ? streamer.clip_min_s : 20), 'aria-label': 'Minimum length in seconds' });
    const maxLen = h('input', { type: 'number', class: 'input input--mono input--w88', min: '1', step: '1', value: String(streamer ? streamer.clip_max_s : 60), 'aria-label': 'Maximum length in seconds' });
    const numbersError = errorLine();
    for (const el of [games, maxClips, minLen, maxLen]) el.addEventListener('input', () => { numbersError.set(''); syncDirty(); });

    // ----- Layout and crop -----
    const resultEl = h('div', { class: 'result', 'aria-hidden': 'true' });
    const resultText = h('p');
    const frameError = h('div', { hidden: true, role: 'alert' });
    const frameBtn = h('button', { type: 'button', class: 'btn' });
    const resetBtn = h('button', { type: 'button', class: 'btn btn--quiet' }, 'Reset to centre crop');
    const cropIntro = h('p', { style: { maxWidth: '60ch' } });

    const editor = createCropEditor({ frame, layout, rects, onChange: (next) => { rects = next; paintCrop(); syncDirty(); } });

    function paintCrop() {
      frameBtn.textContent = editor.hasFrame() ? 'Use another frame' : 'Get sample frame';
      cropIntro.textContent = layout === 'stacked'
        ? 'Drag a rectangle over each area on the sample frame. Stacked needs two: the facecam and the gameplay.'
        : 'Drag the rectangle over the part of the frame to keep. Fill frame needs one.';
      renderResult(resultEl, { frame: editor.hasFrame() ? frame : null, layout, rects, captionStyle, showHeadline });
      resultText.textContent = layout === 'stacked'
        ? 'The facecam fills the top of the clip and the gameplay fills the rest. Each rectangle keeps the shape of its slot while you drag, so nothing is stretched.'
        : 'The marked area is scaled to fill the whole clip. The rectangle keeps the 9:16 shape while you drag, so nothing is stretched.';
      const marked = layout === 'stacked' ? rects.facecam || rects.gameplay : rects.crop;
      resetBtn.disabled = !marked;
    }

    const layoutSet = layoutChoices({
      value: layout,
      legendClass: 'label',
      onChange: (next) => { layout = next; editor.setLayout(next); paintCrop(); syncDirty(); },
    });

    const captionSet = captionChoices({
      value: captionStyle,
      legendClass: 'label',
      onChange: (next) => { captionStyle = next; paintCrop(); syncDirty(); },
    });
    const headlineSwitch = headlineToggle({
      value: showHeadline,
      onChange: (next) => { showHeadline = next; paintCrop(); syncDirty(); },
    });

    // ----- Voiceover -----
    const voiceKeyNote = h('p', { class: 'help', hidden: true },
      'Writing narrations automatically needs an API key. Until one is saved, clips keep the stream\'s own sound unless you type a narration by hand. ',
      h('a', { href: '#/settings' }, 'Add a key in Settings'), '.');
    const voiceCtl = voiceoverControls({
      value: voice,
      voices,
      legendClass: 'label',
      onChange: (next) => { voice = next; paintVoice(); syncDirty(); },
    });
    function paintVoice() {
      const state = ctx.state();
      voiceCtl.optionsEl.hidden = !voice.on;
      voiceKeyNote.hidden = !(state && state.llm_ready === false);
    }
    stopState = ctx.onState(() => { if (alive) paintVoice(); });
    const voiceSet = h('fieldset', { class: 'fieldset', style: { gap: '12px' } },
      h('legend', { class: 'label' }, 'Voiceover'),
      voiceCtl.toggleEl, voiceKeyNote, voiceCtl.optionsEl);

    frameBtn.addEventListener('click', async () => {
      const url = link.value.trim();
      frameError.hidden = true;
      if (!url) {
        linkError.set('Paste the channel link first. The frame comes from that channel.');
        link.focus();
        return;
      }
      // The frame should show the game this streamer is clipped in, so the Games field goes along.
      const wanted = games.value.trim();
      editor.setBusy(wanted
        ? `Looking for a recent stream in ${wanted} to take a frame from. This takes 5 to 30 seconds.`
        : 'Getting a frame from the latest finished stream. This takes 5 to 20 seconds.');
      try {
        const result = await withBusy(frameBtn, () => api.streamers.sampleFrame(url, wanted));
        if (!alive) return;
        frame = { url: result.url, width: result.width, height: result.height };
        frameToken = result.frame_token ?? null;
        editor.setFrame(frame);
        if (result.note) {
          frameError.hidden = false;
          replace(frameError, notice('warn', result.note));
        }
      } catch (err) {
        if (!alive) return;
        editor.setBusy(null);
        frameError.hidden = false;
        replace(frameError, notice('warn', `${err.message} You can still save: the clip uses a centre crop until an area is marked.`));
      }
      paintCrop();
      syncDirty();
    });
    resetBtn.addEventListener('click', () => editor.reset());

    // ----- Destination, enabled -----
    const destSelect = h('select', { class: 'input', id: 's-dest' },
      dests.map((d) => h('option', { value: String(d.id) }, fmt.destName(d))),
      h('option', { value: '' }, 'No default'));
    destSelect.value = streamer && streamer.default_destination_id != null ? String(streamer.default_destination_id) : '';
    if (destSelect.selectedIndex < 0) destSelect.value = '';
    destSelect.addEventListener('change', syncDirty);
    platform.addEventListener('change', syncDirty);

    const watching = toggle({
      pressed: streamer ? streamer.enabled : true,
      label: (on) => (on ? 'Watching this streamer' : 'Paused. Not watching for new streams'),
      onToggle: () => syncDirty(),
    });

    // ----- Footer -----
    const submitBtn = h('button', { type: 'submit', class: 'btn btn--primary' }, editing ? 'Save changes' : 'Add streamer');
    const saveError = h('div', { hidden: true });
    const deleteSlot = h('div', { class: 'confirm' });

    function paintDelete(confirming) {
      if (!editing) return;
      if (!confirming) {
        replace(deleteSlot, h('button', { type: 'button', class: 'btn btn--danger', onclick: () => paintDelete(true) }, 'Delete streamer'));
        return;
      }
      const yes = h('button', { type: 'button', class: 'btn btn--danger' }, `Delete ${streamer.name}`);
      const no = h('button', { type: 'button', class: 'btn', onclick: () => { paintDelete(false); deleteSlot.querySelector('button').focus(); } }, 'Keep');
      yes.addEventListener('click', async () => {
        try {
          await withBusy([yes, no], () => api.streamers.remove(id));
          if (!alive) return;
          ctx.refreshState();
          ctx.navigate('#/streamers?saved=deleted');
        } catch (err) {
          if (!alive) return;
          saveError.hidden = false;
          replace(saveError, h('div', { role: 'alert' }, notice('danger', err.message)));
          paintDelete(false);
        }
      });
      replace(deleteSlot, h('span', { class: 'confirm__text', role: 'alert' }, 'Delete this streamer? This cannot be undone.'), yes, no);
      no.focus();
    }

    // ----- Read, validate, save -----
    function read() {
      const linkText = link.value.trim();
      const data = readFields(linkText);
      // Only name a voice once one is known; the server has its own default.
      if (voice.voice) data.voice = voice.voice;
      return data;
    }
    function readFields(linkText) {
      // Stacked needs both boxes. Once a frame is loaded the editor draws each
      // unmarked box at its centred default, so save what is on screen instead
      // of sending nothing and having the server refuse the form.
      const shown = (key) => rects[key] ?? (layout === 'stacked' && frame ? centreRect(key, frame) : null);
      return {
        channel_url: resolved && resolvedFor === linkText && resolved.channel_url ? resolved.channel_url : linkText,
        platform: platform.value,
        name: name.value.trim(),
        mode: mode(),
        instructions: instructions.value.trim(),
        games: games.value.trim(),
        max_clips_per_vod: Number(maxClips.value),
        clip_min_s: Number(minLen.value),
        clip_max_s: Number(maxLen.value),
        layout,
        caption_style: captionStyle,
        show_headline: showHeadline,
        voiceover: voice.on,
        voiceover_mix: voice.mix,
        crop_rect: rects.crop,
        facecam_rect: shown('facecam'),
        gameplay_rect: shown('gameplay'),
        // If the destinations did not load, leave the saved default alone.
        default_destination_id: destResult.error
          ? (streamer ? streamer.default_destination_id ?? null : null)
          : (destSelect.value ? Number(destSelect.value) : null),
        enabled: watching.isPressed(),
      };
    }
    const snapshot = () => JSON.stringify([read(), frameToken]);
    const isDirty = () => snapshot() !== savedSnapshot;

    function syncDirty() {
      if (!editing) return;
      testBtn.textContent = isDirty() ? 'Save and test on last VOD' : 'Test on last VOD';
    }
    function syncSubmit() {
      const blocked = Boolean(resolved && resolved.supported === false && resolvedFor === link.value.trim());
      submitBtn.disabled = blocked;
    }

    function validate(data) {
      let first = null;
      const fail = (line, message, control) => { line.set(message); if (!first) first = control; };
      linkError.set(''); nameError.set(''); instrError.set(''); numbersError.set('');
      if (!data.channel_url) fail(linkError, 'Paste the channel link.', link);
      if (!data.name) fail(nameError, 'Give the streamer a name.', name);
      if (data.mode === 'custom' && !data.instructions) fail(instrError, 'Custom needs instructions: describe what to look for.', instructions);
      const whole = (n) => Number.isInteger(n) && n > 0;
      if (!whole(data.max_clips_per_vod)) fail(numbersError, 'Max clips per VOD has to be a whole number, 1 or more.', maxClips);
      else if (!whole(data.clip_min_s) || !whole(data.clip_max_s)) fail(numbersError, 'Clip length has to be whole seconds, 1 or more.', minLen);
      else if (data.clip_min_s > data.clip_max_s) fail(numbersError, 'The minimum clip length is longer than the maximum.', minLen);
      if (first) first.focus();
      return !first;
    }

    /** Returns true when the streamer was saved. */
    async function save({ stay = false } = {}) {
      const data = read();
      saveError.hidden = true;
      if (!validate(data)) return false;
      if (frameToken) data.frame_token = frameToken;
      try {
        const saved = await withBusy(submitBtn, () => (editing ? api.streamers.update(id, data) : api.streamers.create(data)));
        if (!alive) return false;
        ctx.refreshState();
        if (stay) {
          if (saved) streamer = saved;
          frameToken = null;
          savedSnapshot = snapshot();
          syncDirty();
          toast('Saved.', { kind: 'ok' });
          return true;
        }
        ctx.navigate(`#/streamers?saved=${editing ? 'saved' : 'added'}`);
        return true;
      } catch (err) {
        if (!alive) return false;
        saveError.hidden = false;
        replace(saveError, h('div', { role: 'alert' }, notice('danger', err.message)));
        return false;
      }
    }

    const form = h('form', { class: 'sform', novalidate: true },
      h('div', { class: 'sform__fields' },
        h('div', { class: 'field' },
          h('label', { class: 'label', for: 's-link' }, 'Channel link'),
          link, linkError, linkStatus),
        field({ label: 'Platform', control: platform, help: 'Filled in from the link. You can change it.' }),
        field({ label: 'Name', control: name, help: 'Filled in from the link. Shown in the queue and used in the credit line.', error: nameError }),
        modeSet,
        h('div', { class: 'field' },
          h('label', { class: 'label', for: 's-instr' }, 'Instructions', instrNote),
          instructions, instrHelp, instrError,
          h('div', { class: 'row row--tight' }, testBtn, testNote),
          testOut),
        field({ label: 'Games', control: games, optional: true, help: 'Only process parts of a stream spent in these games. Leave empty to process everything.' }),
        h('div', { class: 'block' },
          h('div', { class: 'row' },
            field({ label: 'Max clips per VOD', control: maxClips }),
            h('fieldset', { class: 'fieldset' },
              h('legend', { class: 'label' }, 'Clip length in seconds'),
              h('div', { class: 'range-pair' }, minLen, h('span', { class: 'muted' }, 'to'), maxLen))),
          numbersError),
        layoutSet,
        captionSet,
        headlineSwitch,
        voiceSet,
        field({ label: 'Default destination', control: destSelect, help: destResult.error
          ? h('span', { class: 'help help--error' }, `Destinations did not load: ${destResult.error.message}`)
          : 'Can be left empty and chosen per clip in review.' }),
        h('div', { class: 'row row--center' }, watching, h('span', { class: 'faint', style: { fontSize: '13px' } }, 'Language: English, fixed in v1')),
        saveError,
        h('div', { class: 'form-foot', style: { gap: '12px' } },
          submitBtn,
          h('a', { class: 'btn', href: '#/streamers' }, 'Cancel'),
          editing ? [h('span', { class: 'form-foot__spacer' }), deleteSlot] : null)),
      h('section', { class: 'sform__crop', 'aria-label': 'Crop area' },
        h('div', { class: 'block', style: { gap: '6px' } },
          h('h2', { class: 'label', style: { fontWeight: '400' } }, 'Crop area'),
          cropIntro),
        editor.el,
        frameError,
        h('div', { class: 'crop-tools' }, frameBtn, resetBtn),
        h('p', { class: 'help' }, 'Drag a rectangle to move it, or a corner to resize it. With the keyboard: Tab to a rectangle, arrow keys move it, Shift with an arrow key resizes it.'),
        h('div', { class: 'crop-result' },
          resultEl,
          h('div', { class: 'crop-result__text' },
            h('h3', { class: 'label', style: { fontWeight: '400' } }, 'Result'),
            resultText,
            h('p', { class: 'faint', style: { fontSize: '13px' } }, 'Until you mark an area, Snipr uses a centre crop. There is no face or action tracking in v1.')))));

    form.addEventListener('submit', (e) => { e.preventDefault(); save(); });

    replace(body, form);
    paintMode();
    paintCrop();
    paintVoice();
    paintDelete(false);
    savedSnapshot = snapshot();
    syncDirty();
    if (!editing) link.focus();
  }

  load();
  return () => {
    alive = false;
    clearTimeout(resolveTimer);
    if (resolveAbort) resolveAbort.abort();
    if (stopState) stopState();
  };
}
