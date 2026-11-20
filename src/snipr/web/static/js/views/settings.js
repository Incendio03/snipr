// Settings: API key, model, processing window, quality bar, posting defaults,
// transcription model and storage. Not in the Designer's mock; it uses the
// same components as the other screens.

import { h, replace } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import { toast, loadingBlock, errorBlock, pageHead, field, errorLine, toggle, notice, withBusy, slotsEditor } from '../ui.js';

const TRANSCRIPTION_MODELS = [
  ['small', 'small · fastest, least accurate'],
  ['medium', 'medium · the default'],
  ['large-v3', 'large-v3 · most accurate, slowest'],
];

export function mount(root, ctx) {
  let alive = true;

  root.classList.add('main--wide-gap');
  const head = pageHead({ title: 'Settings', sub: 'These apply to every streamer. Per-streamer choices live on the streamer.' });
  const body = h('div');
  root.append(head.el, body);

  async function load() {
    replace(body, loadingBlock('Loading settings'));
    try {
      const settings = await api.settings.get();
      if (!alive) return;
      build(settings ?? {});
    } catch (err) {
      if (alive) replace(body, errorBlock(err.message, load));
    }
  }

  function section(name, intro, ...content) {
    return h('section', { class: 'settings__section', 'aria-label': name },
      h('div', { class: 'settings__intro' }, h('h2', { class: 'settings__name' }, name), intro ? h('p', { class: 'help', style: { fontSize: '13px' } }, intro) : null),
      h('div', { class: 'settings__body' }, content));
  }

  function build(initial) {
    let saved = initial;

    // ----- API keys: one block per provider -----
    // Not a <form>: each block sits inside the settings form, and its key is
    // saved on its own, by its button or by Enter in the field.
    function keyBlockFor({ provider, label, placeholder, initiallySet }) {
      const keyStatus = h('div', { role: 'status' });
      const keyInput = h('input', { type: 'password', class: 'input input--mono', placeholder, autocomplete: 'off', spellcheck: 'false' });
      const keyError = errorLine();
      const keySave = h('button', { type: 'button', class: 'btn' }, 'Save key');
      const keyRemoveSlot = h('div', { class: 'confirm' });
      let keySet = Boolean(initiallySet);

      function paint(confirming = false) {
        replace(keyStatus, keySet
          ? h('span', { class: 'chip chip--ok' }, 'A key is saved')
          : h('span', { class: 'chip chip--warn' }, 'No key saved'));
        keyInput.placeholder = keySet ? 'Paste a new key to replace the saved one' : placeholder;
        keySave.textContent = keySet ? 'Replace key' : 'Save key';
        if (!keySet) { replace(keyRemoveSlot); return; }
        if (!confirming) {
          replace(keyRemoveSlot, h('button', { type: 'button', class: 'btn btn--danger', onclick: () => paint(true) }, 'Remove key'));
          return;
        }
        const yes = h('button', { type: 'button', class: 'btn btn--danger' }, 'Remove the key');
        const no = h('button', { type: 'button', class: 'btn', onclick: () => { paint(false); keyRemoveSlot.querySelector('button').focus(); } }, 'Keep');
        yes.addEventListener('click', async () => {
          keyError.set('');
          try {
            const result = await withBusy([yes, no], () => api.settings.removeKey(provider));
            if (!alive) return;
            keySet = Boolean(result && result.key_set);
            toast('Key removed.', { kind: 'ok' });
            ctx.refreshState();
          } catch (err) {
            if (alive) keyError.set(err.message);
          }
          if (alive) paint(false);
        });
        replace(keyRemoveSlot, h('span', { class: 'confirm__text', role: 'alert' }, 'Informative and Custom modes stop working without a key for the provider in use.'), yes, no);
        no.focus();
      }

      async function saveKey() {
        keyError.set('');
        const key = keyInput.value.trim();
        if (!key) { keyError.set('Paste the key first.'); keyInput.focus(); return; }
        try {
          const result = await withBusy(keySave, () => api.settings.setKey(key, provider));
          if (!alive) return;
          keySet = Boolean(result ? result.key_set : true);
          keyInput.value = '';
          toast('Key saved.', { kind: 'ok' });
          ctx.refreshState();
          paint(false);
        } catch (err) {
          if (alive) keyError.set(err.message);
        }
      }
      keySave.addEventListener('click', saveKey);
      keyInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); saveKey(); }
      });

      const el = h('div', { class: 'block', style: { gap: '12px' } },
        keyStatus,
        h('div', { class: 'key-row' }, field({ label, control: keyInput }), keySave),
        keyError,
        keyRemoveSlot);
      return { el, paint };
    }
    const claudeKey = keyBlockFor({ provider: 'anthropic', label: 'Anthropic API key', placeholder: 'sk-ant-…', initiallySet: saved.llm && saved.llm.key_set });
    const geminiKey = keyBlockFor({ provider: 'gemini', label: 'Gemini API key', placeholder: 'AIza…', initiallySet: saved.llm && saved.llm.gemini_key_set });

    // ----- Controls for PUT /api/settings -----
    const text = (value, cls = '') => h('input', { type: 'text', class: `input ${cls}`, value: value ?? '', autocomplete: 'off', spellcheck: 'false' });
    const number = (value, attrs = {}) => h('input', { type: 'number', class: 'input input--mono input--w120', value: value ?? '', ...attrs });

    const model = text(saved.llm && saved.llm.model, 'input--mono');
    const geminiModel = text(saved.llm && saved.llm.gemini_model, 'input--mono');
    const provider = h('select', { class: 'input', id: 'set-provider' },
      h('option', { value: 'anthropic' }, 'Claude (Anthropic)'),
      h('option', { value: 'gemini' }, 'Gemini (Google)'));
    provider.value = (saved.llm && saved.llm.provider) || 'anthropic';
    const claudePane = h('div', { class: 'block', style: { gap: '16px' } });
    const geminiPane = h('div', { class: 'block', style: { gap: '16px' } });
    function paintProvider() {
      claudePane.hidden = provider.value !== 'anthropic';
      geminiPane.hidden = provider.value !== 'gemini';
    }
    provider.addEventListener('change', paintProvider);

    // "Test connection" asks the provider that is saved, so unsaved changes are saved first by the user.
    const testBtn = h('button', { type: 'button', class: 'btn' }, 'Test connection');
    const testResult = h('div', { role: 'status' });
    testBtn.addEventListener('click', async () => {
      replace(testResult);
      if (Object.keys(read(false).patch).length) {
        replace(testResult, notice('warn', 'Save your settings first. The test uses the provider and model that are saved.'));
        return;
      }
      try {
        const result = await withBusy(testBtn, () => api.settings.testAi());
        if (alive) replace(testResult, notice('ok', (result && result.message) || 'It works.'));
      } catch (err) {
        if (alive) replace(testResult, notice('danger', err.message));
      }
    });
    const windowStart = text(saved.processing && saved.processing.window_start, 'input--mono input--w120');
    const windowEnd = text(saved.processing && saved.processing.window_end, 'input--mono input--w120');
    for (const el of [windowStart, windowEnd]) { el.placeholder = 'HH:MM'; el.maxLength = 5; }
    const pollMinutes = number(saved.processing && saved.processing.poll_minutes, { min: '1', step: '1' });
    const maxRetries = number(saved.processing && saved.processing.max_retries, { min: '0', step: '1' });
    const minScore = number(saved.selection && saved.selection.min_score, { min: '0', max: '1', step: '0.05' });
    const slots = slotsEditor((saved.publishing && saved.publishing.default_post_slots) ?? []);
    const credit = toggle({
      pressed: Boolean(saved.publishing && saved.publishing.append_streamer_credit),
      label: (on) => (on ? 'Add the streamer credit line to every description' : 'No credit line is added'),
      onToggle: () => sync(),
    });
    const transcription = h('select', { class: 'input' }, TRANSCRIPTION_MODELS.map(([value, label]) => h('option', { value }, label)));
    const currentModel = saved.transcription && saved.transcription.model;
    if (currentModel && !TRANSCRIPTION_MODELS.some(([value]) => value === currentModel)) {
      transcription.append(h('option', { value: currentModel }, currentModel));
    }
    transcription.value = currentModel ?? 'medium';
    const sourceDays = number(saved.storage && saved.storage.source_max_age_days, { min: '1', step: '1' });
    const rejectedDays = number(saved.storage && saved.storage.keep_rejected_clips_days, { min: '0', step: '1' });

    const errors = { model: errorLine(), window: errorLine(), poll: errorLine(), score: errorLine(), storage: errorLine() };
    const storageFacts = h('dl', { class: 'kv' });
    function paintStorage() {
      const st = saved.storage ?? {};
      replace(storageFacts,
        h('dt', null, 'Data folder'), h('dd', null, st.data_dir || '–'),
        h('dt', null, 'Used by Snipr'), h('dd', null, fmt.gb(st.used_gb)),
        h('dt', null, 'Free on the drive'), h('dd', null, fmt.gb(st.free_gb)));
    }

    const saveBtn = h('button', { type: 'submit', class: 'btn btn--primary' }, 'Save settings');
    const saveNote = h('span', { class: 'help', role: 'status', style: { fontSize: '13px' } });
    const saveError = h('div', { hidden: true });

    /** Read the form. Returns { patch } with only what changed, or { invalid: control }. */
    function read(show) {
      const patch = {};
      let invalid = null;
      const fail = (line, message, control) => { if (show) line.set(message); if (!invalid) invalid = control; };
      const put = (group, key, value, before) => {
        if (JSON.stringify(value) === JSON.stringify(before)) return;
        patch[group] = patch[group] ?? {};
        patch[group][key] = value;
      };
      if (show) for (const line of Object.values(errors)) line.set('');
      const s = saved;

      put('llm', 'provider', provider.value, (s.llm && s.llm.provider) || 'anthropic');
      const modelName = model.value.trim();
      const geminiName = geminiModel.value.trim();
      if (!modelName) fail(errors.model, 'Write the model name, for example claude-haiku-5-5.', model);
      else put('llm', 'model', modelName, s.llm && s.llm.model);
      if (!geminiName) fail(errors.model, 'Write the Gemini model name, for example gemini-3.5-flash-lite.', geminiModel);
      else put('llm', 'gemini_model', geminiName, s.llm && s.llm.gemini_model);

      const start = fmt.normaliseHHMM(windowStart.value);
      const end = fmt.normaliseHHMM(windowEnd.value);
      if (!start || !end) fail(errors.window, 'Write both times as 24 hour HH:MM, for example 01:00.', start ? windowEnd : windowStart);
      else {
        put('processing', 'window_start', start, s.processing && s.processing.window_start);
        put('processing', 'window_end', end, s.processing && s.processing.window_end);
      }

      const poll = Number(pollMinutes.value);
      const retries = Number(maxRetries.value);
      if (pollMinutes.value === '' || !Number.isInteger(poll) || poll < 1) fail(errors.poll, 'The check interval has to be a whole number of minutes, 1 or more.', pollMinutes);
      else if (maxRetries.value === '' || !Number.isInteger(retries) || retries < 0) fail(errors.poll, 'Retries has to be a whole number, 0 or more.', maxRetries);
      else {
        put('processing', 'poll_minutes', poll, s.processing && s.processing.poll_minutes);
        put('processing', 'max_retries', retries, s.processing && s.processing.max_retries);
      }

      const score = Number(minScore.value);
      if (minScore.value === '' || Number.isNaN(score) || score < 0 || score > 1) fail(errors.score, 'The quality bar is a number from 0 to 1, for example 0.6.', minScore);
      else put('selection', 'min_score', score, s.selection && s.selection.min_score);

      const before = (s.publishing && s.publishing.default_post_slots) ?? [];
      if (show) {
        const list = slots.read();
        if (!list) { if (!invalid) invalid = slots; } else put('publishing', 'default_post_slots', list, [...before].sort());
      } else if (JSON.stringify(slots.peek()) !== JSON.stringify(before)) {
        patch.publishing = { ...(patch.publishing ?? {}), default_post_slots: slots.peek() };
      }
      put('publishing', 'append_streamer_credit', credit.isPressed(), Boolean(s.publishing && s.publishing.append_streamer_credit));
      put('transcription', 'model', transcription.value, s.transcription && s.transcription.model);

      const src = Number(sourceDays.value);
      const rej = Number(rejectedDays.value);
      if (sourceDays.value === '' || !Number.isInteger(src) || src < 1) fail(errors.storage, 'Source streams are kept for a whole number of days, 1 or more.', sourceDays);
      else if (rejectedDays.value === '' || !Number.isInteger(rej) || rej < 0) fail(errors.storage, 'Rejected clips are kept for a whole number of days, 0 or more.', rejectedDays);
      else {
        put('storage', 'source_max_age_days', src, s.storage && s.storage.source_max_age_days);
        put('storage', 'keep_rejected_clips_days', rej, s.storage && s.storage.keep_rejected_clips_days);
      }
      return { patch, invalid };
    }

    function sync() {
      const { patch, invalid } = read(false);
      const dirty = Object.keys(patch).length > 0 || Boolean(invalid);
      saveBtn.disabled = !dirty;
      saveNote.textContent = dirty ? 'You have changes that are not saved.' : 'Everything is saved.';
    }

    const form = h('form', { novalidate: true, class: 'settings' },
      section('AI provider', 'The AI reads transcripts to pick moments and writes headlines, post text and narration. Without a key, Highlights still works on chat and loudness alone. Keys are kept in Windows Credential Manager and are never shown again.',
        field({ label: 'Use', control: provider, help: 'You can save a key for both and switch between them here.' }),
        claudePane,
        geminiPane,
        errors.model,
        h('div', { class: 'block', style: { gap: '10px' } }, h('div', null, testBtn), testResult)),
      section('Processing', 'Downloading, transcription and encoding only run inside this window, unless you press Run now in Activity.',
        h('div', { class: 'block' },
          h('div', { class: 'row' },
            field({ label: 'Window opens', control: windowStart }),
            field({ label: 'Window closes', control: windowEnd })),
          h('span', { class: 'help' }, '24 hour local time. A window that closes earlier than it opens runs past midnight.'),
          errors.window),
        h('div', { class: 'block' },
          h('div', { class: 'row' },
            field({ label: 'Check for new streams every (minutes)', control: pollMinutes }),
            field({ label: 'Automatic retries per job', control: maxRetries })),
          errors.poll)),
      section('Quality bar', 'The lowest score a moment can have and still become a clip.',
        field({ label: 'Minimum score, 0 to 1', control: minScore, help: 'Raise it for fewer, stronger clips. Lower it if quiet streams produce nothing.', error: errors.score })),
      section('Publishing', 'What a new destination starts with.',
        h('div', { class: 'block', style: { gap: '6px' } }, h('span', { class: 'label' }, 'Default posting slots, 24 hour local time'), slots),
        credit),
      section('Transcription', 'Larger models are more accurate and take longer on this PC.',
        field({ label: 'Speech-to-text model', control: transcription })),
      section('Storage', 'Source streams are large, so they are deleted once their clips are reviewed or they get too old.',
        storageFacts,
        h('div', { class: 'block' },
          h('div', { class: 'row' },
            field({ label: 'Delete source streams after (days)', control: sourceDays }),
            field({ label: 'Keep rejected clips for (days)', control: rejectedDays })),
          errors.storage)),
      saveError,
      h('div', { class: 'savebar' }, saveBtn, saveNote));

    form.addEventListener('input', sync);
    form.addEventListener('change', sync);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      saveError.hidden = true;
      const { patch, invalid } = read(true);
      if (invalid) { if (invalid.focus) invalid.focus(); return; }
      if (!Object.keys(patch).length) { sync(); return; }
      try {
        const next = await withBusy(saveBtn, () => api.settings.update(patch));
        if (!alive) return;
        if (next) saved = next;
        else saved = mergePatch(saved, patch);
        paintStorage();
        toast('Settings saved.', { kind: 'ok' });
        ctx.refreshState();
        sync();
      } catch (err) {
        if (!alive) return;
        saveError.hidden = false;
        replace(saveError, h('div', { role: 'alert' }, notice('danger', err.message)));
      }
    });

    claudePane.append(
      claudeKey.el,
      field({ label: 'Claude model', control: model, help: 'Paid per use from Anthropic Console credit. A 3 hour stream costs around a cent or two on the small model.' }));
    geminiPane.append(
      geminiKey.el,
      field({ label: 'Gemini model', control: geminiModel, help: 'gemini-3.5-flash-lite is the default because it reliably answers on the free tier. gemini-3.8-flash and gemini-3.6-flash are stronger but are often overloaded there; if the chosen model is too busy, Snipr falls back to a Flash-Lite model by itself. Get a key in Google AI Studio (aistudio.google.com), under "Get API key".' }),
      notice('warn', (saved.llm && saved.llm.gemini_note) || 'On the free tier, Google uses what is sent to improve its products.'));

    replace(body, form);
    claudeKey.paint(false);
    geminiKey.paint(false);
    paintProvider();
    paintStorage();
    sync();
  }

  load();
  return () => { alive = false; };
}

function mergePatch(base, patch) {
  const out = { ...base };
  for (const [group, values] of Object.entries(patch)) out[group] = { ...(base[group] ?? {}), ...values };
  return out;
}
