// Crop marking on a sample frame. Rectangles are stored as fractions
// [x, y, w, h] of the source frame and keep the shape of their slot in the
// 1080 x 1920 clip, so nothing is stretched. null means "not marked": the
// rectangle is then drawn dashed at the centre crop the renderer falls back to.

import { h, replace } from './dom.js';
import { busyBar } from './ui.js';

const SLOTS = {
  crop: { label: 'Crop', name: 'Crop', ratio: 1080 / 1920 },
  facecam: { label: '1 Facecam', name: 'Facecam', ratio: 1080 / 608 },
  gameplay: { label: '2 Gameplay', name: 'Gameplay', ratio: 1080 / 1312 },
};
const KEYS_FOR = { fill: ['crop'], stacked: ['gameplay', 'facecam'] };
const MIN_W = 0.06;
const round4 = (n) => Math.round(n * 10000) / 10000;
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

/** Width / height of a slot in fraction space for a frame of the given size. */
function fractionRatio(key, frame) {
  return SLOTS[key].ratio * (frame.height / frame.width);
}

/** The largest centred rectangle of the slot's shape. */
export function centreRect(key, frame) {
  const k = fractionRatio(key, frame);
  let w = Math.min(1, k);
  let ht = w / k;
  if (ht > 1) { ht = 1; w = k; }
  return [(1 - w) / 2, (1 - ht) / 2, w, ht];
}

function validRect(rect) {
  return Array.isArray(rect) && rect.length === 4 && rect.every((n) => typeof n === 'number' && Number.isFinite(n)) && rect[2] > 0 && rect[3] > 0;
}

/**
 * createCropEditor({ frame, layout, rects, onChange })
 *   frame: { url, width, height } or null
 *   rects: { crop, facecam, gameplay } each [x, y, w, h] or null
 * Returns { el, setFrame, setLayout, setBusy, setMessage, getRects, reset }.
 */
export function createCropEditor({ frame = null, layout = 'fill', rects = {}, onChange }) {
  const state = {
    frame,
    layout,
    busy: null,
    message: null,
    rects: {
      crop: validRect(rects.crop) ? [...rects.crop] : null,
      facecam: validRect(rects.facecam) ? [...rects.facecam] : null,
      gameplay: validRect(rects.gameplay) ? [...rects.gameplay] : null,
    },
  };
  const stage = h('div', { class: 'crop-stage' });
  const boxes = {};
  let top = null;

  const shown = (key) => state.rects[key] ?? centreRect(key, state.frame);

  function place(key) {
    const box = boxes[key];
    if (!box) return;
    const [x, y, w, ht] = shown(key);
    box.style.left = `${x * 100}%`;
    box.style.top = `${y * 100}%`;
    box.style.width = `${w * 100}%`;
    box.style.height = `${ht * 100}%`;
    const marked = Boolean(state.rects[key]);
    box.classList.toggle('crop-rect--unmarked', !marked);
    box.querySelector('.crop-rect__label').textContent = marked ? SLOTS[key].label : `${SLOTS[key].label} · centre`;
    box.classList.toggle('crop-rect--top', top === key);
  }

  function commit(key, rect) {
    state.rects[key] = rect.map(round4);
    place(key);
    if (onChange) onChange(getRects());
  }

  function raise(key) {
    top = key;
    for (const k of Object.keys(boxes)) boxes[k].classList.toggle('crop-rect--top', k === key);
  }

  function startDrag(key, mode, event) {
    if (event.button !== 0) return;
    event.preventDefault();
    const box = boxes[key];
    box.focus({ preventScroll: true });
    raise(key);
    const bounds = stage.getBoundingClientRect();
    const k = fractionRatio(key, state.frame);
    const [x0, y0, w0, h0] = shown(key);
    const px0 = (event.clientX - bounds.left) / bounds.width;
    const py0 = (event.clientY - bounds.top) / bounds.height;
    const minW = Math.min(MIN_W, k, 1);

    function onMove(e) {
      const px = (e.clientX - bounds.left) / bounds.width;
      const py = (e.clientY - bounds.top) / bounds.height;
      if (mode === 'move') {
        commit(key, [clamp(x0 + px - px0, 0, 1 - w0), clamp(y0 + py - py0, 0, 1 - h0), w0, h0]);
        return;
      }
      // Resize from a corner; the opposite corner stays where it is.
      const dirX = mode.includes('e') ? 1 : -1;
      const dirY = mode.includes('s') ? 1 : -1;
      const ax = dirX > 0 ? x0 : x0 + w0;
      const ay = dirY > 0 ? y0 : y0 + h0;
      const roomW = dirX > 0 ? 1 - ax : ax;
      const roomH = dirY > 0 ? 1 - ay : ay;
      const maxW = Math.min(roomW, roomH * k);
      const wanted = Math.max((px - ax) * dirX, (py - ay) * dirY * k);
      const w = clamp(wanted, Math.min(minW, maxW), maxW);
      const ht = w / k;
      commit(key, [dirX > 0 ? ax : ax - w, dirY > 0 ? ay : ay - ht, w, ht]);
    }
    function onUp(e) {
      box.releasePointerCapture(e.pointerId);
      box.removeEventListener('pointermove', onMove);
      box.removeEventListener('pointerup', onUp);
      box.removeEventListener('pointercancel', onUp);
    }
    box.setPointerCapture(event.pointerId);
    box.addEventListener('pointermove', onMove);
    box.addEventListener('pointerup', onUp);
    box.addEventListener('pointercancel', onUp);
  }

  function onKey(key, e) {
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    let [x, y, w, ht] = shown(key);
    const k = fractionRatio(key, state.frame);
    let grow = 0;
    if (e.key === '+' || e.key === '=') grow = 1;
    else if (e.key === '-' || e.key === '_') grow = -1;
    else if (arrows[e.key] && e.shiftKey) grow = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 1 : -1;
    else if (arrows[e.key]) {
      const step = 0.005;
      x = clamp(x + arrows[e.key][0] * step, 0, 1 - w);
      y = clamp(y + arrows[e.key][1] * step, 0, 1 - ht);
    } else return;
    e.preventDefault();
    if (grow) {
      // Resize around the centre, then push back inside the frame.
      const maxW = Math.min(1, k);
      const nw = clamp(w + grow * 0.01, Math.min(MIN_W, maxW), maxW);
      const nh = nw / k;
      x = clamp(x - (nw - w) / 2, 0, 1 - nw);
      y = clamp(y - (nh - ht) / 2, 0, 1 - nh);
      w = nw;
      ht = nh;
    }
    raise(key);
    commit(key, [x, y, w, ht]);
  }

  function buildBox(key) {
    const slot = SLOTS[key];
    const box = h('div', {
      class: `crop-rect crop-rect--${key}${state.layout === 'fill' ? ' crop-rect--dim' : ''}`,
      tabindex: '0', role: 'group',
      'aria-label': `${slot.name} rectangle. Arrow keys move it. Shift with an arrow key, or plus and minus, resize it.`,
    },
    h('span', { class: 'crop-rect__label' }, slot.label),
    ['ne', 'se', 'sw'].map((corner) => h('span', { class: 'crop-rect__handle', 'aria-hidden': 'true', dataset: { handle: corner } })));
    box.addEventListener('pointerdown', (e) => {
      const handle = e.target.closest('[data-handle]');
      startDrag(key, handle ? handle.dataset.handle : 'move', e);
    });
    box.addEventListener('keydown', (e) => onKey(key, e));
    box.addEventListener('focus', () => raise(key));
    return box;
  }

  function render() {
    for (const key of Object.keys(boxes)) delete boxes[key];
    stage.classList.toggle('crop-stage--has-frame', Boolean(state.frame));
    if (state.busy) {
      stage.style.aspectRatio = state.frame ? `${state.frame.width} / ${state.frame.height}` : '16 / 9';
      replace(stage, h('div', { class: 'crop-stage__empty', role: 'status' }, h('span', { class: 'micro' }, 'Sample frame'), busyBar(state.busy), h('p', null, state.busy)));
      return;
    }
    if (!state.frame) {
      stage.style.aspectRatio = '16 / 9';
      replace(stage, h('div', { class: 'crop-stage__empty' },
        h('span', { class: 'micro' }, 'No sample frame yet'),
        h('p', null, state.message ?? 'Get a frame from the latest finished stream to mark the crop on it.')));
      return;
    }
    stage.style.aspectRatio = `${state.frame.width} / ${state.frame.height}`;
    const img = h('img', { class: 'crop-stage__img', src: state.frame.url, alt: 'Sample frame from the stream', draggable: 'false' });
    replace(stage, img, h('div', { class: 'crop-stage__tag' }, `Sample frame · ${state.frame.width} × ${state.frame.height}`));
    for (const key of KEYS_FOR[state.layout]) {
      boxes[key] = buildBox(key);
      stage.append(boxes[key]);
      place(key);
    }
  }

  function getRects() {
    return {
      crop: state.rects.crop ? [...state.rects.crop] : null,
      facecam: state.rects.facecam ? [...state.rects.facecam] : null,
      gameplay: state.rects.gameplay ? [...state.rects.gameplay] : null,
    };
  }

  render();

  return {
    el: stage,
    getRects,
    hasFrame: () => Boolean(state.frame),
    setFrame(next) { state.frame = next; state.busy = null; state.message = null; render(); },
    setLayout(next) { state.layout = next; top = null; render(); },
    /** Show a progress message in place of the frame, or clear it with null. */
    setBusy(text) { state.busy = text; render(); },
    /** Message for the empty stage, e.g. why a frame could not be fetched. */
    setMessage(text) { state.message = text; render(); },
    /** Clear the rectangles of the current layout back to "not marked". */
    reset() {
      for (const key of KEYS_FOR[state.layout]) state.rects[key] = null;
      render();
      if (onChange) onChange(getRects());
    },
  };
}

/**
 * The small 9:16 "Result" picture: what the crop looks like in the clip, with
 * the headline and captions where the clip format puts them.
 */
export function renderResult(el, { frame, layout, rects, captionStyle = 'box', showHeadline = true }) {
  el.className = `result result--${layout} result--${captionStyle === 'bold' ? 'bold' : 'box'}`;
  const part = (key, cls, text) => {
    const box = h('div', { class: `result__part${cls ? ` ${cls}` : ''}` });
    if (frame) {
      const [x, y, w, ht] = validRect(rects[key]) ? rects[key] : centreRect(key, frame);
      box.append(h('img', {
        src: frame.url, alt: '', draggable: 'false',
        style: { width: `${100 / w}%`, height: `${100 / ht}%`, left: `${(-x / w) * 100}%`, top: `${(-y / ht) * 100}%` },
      }));
    } else {
      box.append(text);
    }
    return box;
  };
  replace(el,
    layout === 'stacked'
      ? [part('facecam', 'result__part--facecam', 'Facecam'), part('gameplay', '', 'Gameplay')]
      : part('crop', '', 'Crop, scaled to fill'),
    showHeadline ? h('div', { class: 'ov-headline' }, h('span', null, 'Headline')) : null,
    captionStyle === 'bold'
      ? h('div', { class: 'ov-caption' }, h('b', null, 'spoken'), h('span', null, 'words'))
      : h('div', { class: 'ov-caption' }, h('span', null, 'spoken'), h('b', null, 'captions'), h('span', null, 'here')));
}
