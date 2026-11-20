// Small shared UI pieces: toasts, state blocks, toggles, pickers.

import { h, icon, replace, uid } from './dom.js';
import * as fmt from './format.js';

// ---------- Toasts ----------

/**
 * Show a short message at the bottom of the page.
 * opts.kind: 'info' | 'ok' | 'error'. opts.action: { label, kbd, run }.
 * Returns { dismiss }.
 */
export function toast(message, opts = {}) {
  const kind = opts.kind ?? 'info';
  const duration = opts.duration ?? (kind === 'error' ? 10000 : 5000);
  const region = document.getElementById('toasts');
  let timer = null;
  let gone = false;

  const el = h('div', { class: `toast toast--${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'toast__msg' }, message));

  function dismiss() {
    if (gone) return;
    gone = true;
    clearTimeout(timer);
    el.remove();
    if (opts.onDismiss) opts.onDismiss();
  }

  if (opts.action) {
    el.append(h('button', {
      type: 'button', class: 'btn btn--small',
      onclick: () => { dismiss(); opts.action.run(); },
    }, opts.action.label, opts.action.kbd ? h('kbd', null, opts.action.kbd) : null));
  }
  el.append(h('button', { type: 'button', class: 'btn btn--text btn--small', 'aria-label': 'Dismiss', onclick: dismiss }, icon('close', 14)));

  // Keep at most three on screen.
  while (region.children.length >= 3) region.firstElementChild.remove();
  region.append(el);

  const arm = () => { timer = setTimeout(dismiss, duration); };
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', arm);
  el.addEventListener('focusin', () => clearTimeout(timer));
  el.addEventListener('focusout', arm);
  arm();
  return { dismiss };
}

export function toastError(err) {
  return toast(err && err.message ? err.message : 'Something went wrong.', { kind: 'error' });
}

// ---------- State blocks ----------

export function loadingBlock(text = 'Loading') {
  return h('div', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), text);
}

export function errorBlock(message, onRetry) {
  return h('div', { class: 'state-block state-block--error', role: 'alert' },
    h('p', { class: 'state-block__title' }, 'This did not load'),
    h('p', { class: 'state-block__text' }, message),
    onRetry ? h('div', { class: 'state-block__actions' }, h('button', { type: 'button', class: 'btn', onclick: onRetry }, 'Try again')) : null);
}

export function emptyBlock(title, text, ...actions) {
  return h('div', { class: 'state-block' },
    h('p', { class: 'state-block__title' }, title),
    text ? h('p', { class: 'state-block__text' }, text) : null,
    actions.length ? h('div', { class: 'state-block__actions' }, actions) : null);
}

export function notice(kind, ...children) {
  return h('div', { class: `notice${kind ? ` notice--${kind}` : ''}` },
    kind === 'warn' || kind === 'danger' ? icon('warn', 14) : null,
    h('span', null, children));
}

export function busyBar(label) {
  return h('div', { class: 'bar bar--busy', role: 'progressbar', 'aria-label': label }, h('div', { class: 'bar__fill' }));
}

/** One line shown on Review and Streamers while no API key is saved. */
export function llmNotice(state) {
  if (!state || state.llm_ready !== false) return null;
  return notice('', 'Informative and Custom modes need an Anthropic API key. Highlights still finds moments from chat and audio. ',
    h('a', { href: '#/settings' }, 'Add a key in Settings'), '.');
}

// ---------- Page header ----------

export function pageHead({ title, sub, back, action }) {
  const subEl = h('p', { class: 'page-sub' }, sub ?? '');
  const el = h('header', { class: 'page-head' },
    h('div', { class: 'page-head__text' },
      back ? h('a', { class: 'back-link', href: back.href }, icon('back', 14), back.label) : null,
      h('h1', { class: 'page-title' }, title),
      subEl),
    action ?? null);
  return { el, setSub: (...content) => replace(subEl, content) };
}

// ---------- Buttons ----------

/**
 * Run an async action with the button disabled while it is in flight.
 * Returns the action's result; errors pass through to the caller.
 */
export async function withBusy(button, action) {
  const buttons = Array.isArray(button) ? button : [button];
  const was = buttons.map((b) => b.disabled);
  for (const b of buttons) { b.disabled = true; b.setAttribute('aria-busy', 'true'); }
  try {
    return await action();
  } finally {
    buttons.forEach((b, i) => { b.disabled = was[i]; b.removeAttribute('aria-busy'); });
  }
}

/** The mock's switch: a button with aria-pressed and a text label. */
export function toggle({ pressed, label, onToggle, key }) {
  const text = h('span');
  const btn = h('button', { type: 'button', class: 'toggle', 'aria-pressed': String(Boolean(pressed)), 'data-key': key },
    h('span', { class: 'toggle__track', 'aria-hidden': 'true' }, h('span', { class: 'toggle__knob' })), text);
  const paint = () => { text.textContent = label(btn.getAttribute('aria-pressed') === 'true'); };
  paint();
  btn.setPressed = (value) => { btn.setAttribute('aria-pressed', String(Boolean(value))); paint(); };
  btn.isPressed = () => btn.getAttribute('aria-pressed') === 'true';
  btn.addEventListener('click', () => {
    const next = !btn.isPressed();
    btn.setPressed(next);
    if (onToggle) onToggle(next, btn);
  });
  return btn;
}

export function copyButton(getText, what) {
  const btn = h('button', { type: 'button', class: 'btn btn--text', 'aria-label': `Copy ${what}` }, 'Copy');
  let timer = null;
  btn.addEventListener('click', async () => {
    try {
      await copyText(getText());
      btn.textContent = 'Copied';
      btn.classList.add('btn--copied');
      clearTimeout(timer);
      timer = setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('btn--copied'); }, 1800);
    } catch {
      toast('Could not copy. Select the text and copy it by hand.', { kind: 'error' });
    }
  });
  return btn;
}

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = h('textarea', { class: 'sr-only', 'aria-hidden': 'true' });
  area.value = text;
  document.body.append(area);
  area.select();
  const ok = document.execCommand('copy');
  area.remove();
  if (!ok) throw new Error('copy failed');
}

// ---------- Form pieces ----------

/** Label + control + optional help line. The control gets an id if it has none. */
export function field({ label, control, help, note, optional, error }) {
  if (!control.id) control.id = uid('f');
  return h('div', { class: 'field' },
    h('label', { class: 'label', for: control.id }, label,
      note ? h('span', { class: 'label__note' }, ` · ${note}`) : null,
      optional ? h('span', { class: 'label__opt' }, ' · optional') : null),
    control,
    help ? (help instanceof Node ? help : h('span', { class: 'help' }, help)) : null,
    error ?? null);
}

/** A hidden-until-needed error line that screen readers announce. */
export function errorLine() {
  const el = h('span', { class: 'help help--error', role: 'alert', hidden: true });
  el.set = (message) => { el.textContent = message ?? ''; el.hidden = !message; };
  return el;
}

/** Fill frame / Stacked radio cards. */
export function layoutChoices({ value, onChange, legendClass = 'micro' }) {
  const name = uid('layout');
  const card = (key, text) => h('label', { class: 'choice choice--layout' },
    h('input', { type: 'radio', name, value: key, checked: value === key, onchange: () => onChange(key) }),
    h('span', { class: `glyph glyph--${key}`, 'aria-hidden': 'true' }, key === 'stacked' ? [h('span'), h('span')] : h('span')),
    h('span', null, text));
  return h('fieldset', { class: 'fieldset' },
    h('legend', { class: legendClass }, 'Layout'),
    h('div', { class: 'choices choices--2' }, card('fill', 'Fill frame'), card('stacked', 'Stacked')));
}

/** Box / Bold radio cards for the spoken captions. */
export function captionChoices({ value, onChange, legendClass = 'micro' }) {
  const name = uid('caption');
  const card = (key, title, desc) => h('label', { class: 'choice choice--card' },
    h('input', { type: 'radio', name, value: key, checked: value === key, onchange: () => onChange(key) }),
    h('span', { class: 'choice__text' },
      h('span', { class: 'choice__title' }, title),
      h('span', { class: 'choice__desc' }, desc)));
  return h('fieldset', { class: 'fieldset' },
    h('legend', { class: legendClass }, 'Caption style'),
    h('div', { class: 'choices choices--2' },
      card('box', 'Box', 'White captions, the spoken word on a purple box'),
      card('bold', 'Bold', 'One or two heavy capitals at a time, the spoken word in yellow')));
}

/** The "Show headline" switch with its one-line explanation. */
export function headlineToggle({ value, onChange }) {
  const btn = toggle({ pressed: value, label: () => 'Show headline', onToggle: (next) => onChange(next) });
  const el = h('div', { class: 'block', style: { gap: '2px' } }, btn,
    h('span', { class: 'help' }, 'A short hook on screen for the whole clip'));
  el.isPressed = btn.isPressed;
  return el;
}

/**
 * Voiceover controls shared by the streamer form and Edit clip: the on/off
 * switch, the voice select and the "Stream sound" choice.
 *   value:  { on, voice, mix }   (mix: 'duck' | 'mute')
 *   voices: the answer of GET /api/voices, or { error } when it did not load
 * Returns { toggleEl, optionsEl, read() }. optionsEl is for the caller to
 * show only while voiceover is on.
 */
export function voiceoverControls({ value, voices, legendClass = 'micro', onChange }) {
  const list = voices && Array.isArray(voices.voices) ? voices.voices : [];
  const state = { on: Boolean(value.on), voice: value.voice ?? null, mix: value.mix === 'mute' ? 'mute' : 'duck' };

  const select = h('select', { class: 'input' },
    list.map((v) => h('option', { value: v.id }, v.label || v.id)));
  // Keep a saved voice that the list does not know, rather than dropping it.
  if (state.voice && !list.some((v) => v.id === state.voice)) select.append(h('option', { value: state.voice }, state.voice));
  if (!select.options.length) select.append(h('option', { value: '' }, 'Default voice'));
  select.value = state.voice ?? (voices && voices.default) ?? select.options[0].value;
  if (select.selectedIndex < 0) select.selectedIndex = 0;
  select.addEventListener('change', () => { state.voice = select.value || null; onChange(read()); });

  const toggleBtn = toggle({
    pressed: state.on,
    label: () => 'Voiceover',
    onToggle: (next) => {
      state.on = next;
      // The voice only becomes a choice once voiceover is on.
      if (next && !state.voice) state.voice = select.value || null;
      onChange(read());
    },
  });
  const toggleEl = h('div', { class: 'block', style: { gap: '2px' } }, toggleBtn,
    h('span', { class: 'help' }, 'A narrator reads a script over the clip. Captions follow the narrator.'));

  const mixName = uid('mix');
  const mixCard = (key, text) => h('label', { class: 'choice' },
    h('input', { type: 'radio', name: mixName, value: key, checked: state.mix === key, onchange: () => { state.mix = key; onChange(read()); } }),
    text);

  let voiceHelp = null;
  if (voices && voices.error) voiceHelp = h('span', { class: 'help help--error' }, `Voices did not load: ${voices.error.message}`);
  else if (voices && voices.model_ready === false) voiceHelp = h('span', { class: 'help help--warn' }, 'The voice model is not ready on this PC yet.');

  const optionsEl = h('div', { class: 'voice-opts' },
    field({ label: 'Voice', control: select, help: voiceHelp }),
    h('fieldset', { class: 'fieldset' },
      h('legend', { class: legendClass === 'micro' ? 'label' : legendClass }, 'Stream sound'),
      h('div', { class: 'choices choices--2' }, mixCard('duck', 'Lowered'), mixCard('mute', 'Muted'))));

  function read() {
    // With voiceover on, always name the voice that the select is showing.
    return { on: state.on, voice: state.voice ?? (state.on ? select.value || null : null), mix: state.mix };
  }
  return { toggleEl, optionsEl, read };
}

/**
 * Destination checkboxes with each destination's next free slot.
 * `selected` is a Set of ids and is changed in place.
 */
export function destinationChecks({ dests, selected, slotPrefix = '', onChange }) {
  const box = h('fieldset', { class: 'fieldset' }, h('legend', { class: 'micro' }, 'Destinations'));
  if (!dests.length) {
    box.append(h('p', { class: 'muted' }, 'No destinations yet. ', h('a', { href: '#/publishing' }, 'Add one in Publishing'),
      '. You can still approve a clip without one.'));
    return box;
  }
  for (const d of dests) {
    const slot = fmt.when(d.next_free_slot);
    box.append(h('label', { class: 'check-row' },
      h('input', {
        type: 'checkbox', checked: selected.has(d.id),
        onchange: (e) => {
          if (e.target.checked) selected.add(d.id); else selected.delete(d.id);
          if (onChange) onChange();
        },
      }),
      h('span', { class: 'check-row__name' }, d.name, ' ', h('span', { class: 'check-row__platform' }, `· ${fmt.label(fmt.PLATFORM, d.platform)}`)),
      slot ? h('span', { class: 'check-row__slot' }, `${slotPrefix}${slot}`) : null));
  }
  return box;
}

/** Editable list of HH:MM posting slots. */
export function slotsEditor(initial = [], { ground = false } = {}) {
  const rows = h('div', { class: 'slots-edit__rows' });
  const error = errorLine();

  function addRow(value = '') {
    const input = h('input', {
      type: 'text', class: `input input--mono${ground ? ' input--ground' : ''}`, value,
      placeholder: 'HH:MM', maxlength: '5', inputmode: 'numeric', 'aria-label': 'Posting slot, 24 hour time',
    });
    const row = h('div', { class: 'slots-edit__row' }, input,
      h('button', {
        type: 'button', class: 'btn btn--text btn--icon', 'aria-label': 'Remove this slot',
        onclick: () => { row.remove(); el.dispatchEvent(new Event('change', { bubbles: true })); },
      }, icon('close', 14)));
    rows.append(row);
    return input;
  }
  initial.forEach((s) => addRow(s));

  const el = h('div', { class: 'slots-edit' }, rows,
    h('div', null, h('button', { type: 'button', class: 'btn btn--text', onclick: () => addRow('').focus() }, icon('plus', 14), 'Add slot')),
    error);

  /** Returns a sorted list of slots, or null (and shows why) when invalid. */
  el.read = () => {
    const out = [];
    for (const input of rows.querySelectorAll('input')) {
      const raw = input.value.trim();
      if (!raw) continue;
      const slot = fmt.normaliseHHMM(raw);
      if (!slot) {
        error.set(`"${raw}" is not a time. Use 24 hour HH:MM, for example 18:00.`);
        input.focus();
        return null;
      }
      if (!out.includes(slot)) out.push(slot);
    }
    if (!out.length) {
      error.set('Add at least one slot.');
      return null;
    }
    error.set('');
    return out.sort();
  };
  /** Current values without validation, for change detection. */
  el.peek = () => [...rows.querySelectorAll('input')].map((i) => i.value.trim()).filter(Boolean);
  return el;
}

export function statusChip(status, labels) {
  return h('span', { class: `status status--${status}` }, fmt.label(labels, status));
}

/** 9:16 thumbnail that falls back to a plain block when there is no image. */
export function thumb(url, size = '') {
  const box = h('span', { class: `thumb${size ? ` thumb--${size}` : ''}`, 'aria-hidden': 'true' });
  if (url) {
    const img = h('img', { src: url, alt: '', loading: 'lazy' });
    img.addEventListener('error', () => img.remove());
    box.append(img);
  }
  return box;
}
