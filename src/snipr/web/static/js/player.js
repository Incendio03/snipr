// The 9:16 clip player used by Review and Edit clip. The rendered file already
// carries the headline and captions, so nothing is drawn over the video. The
// headline plate is only shown on the placeholder while a clip renders.

import { h, icon, replace } from './dom.js';
import * as fmt from './format.js';
import { busyBar } from './ui.js';

/**
 * createPlayer(clip) -> { el, toggle(), destroy(), key, onTime(fn) }
 * `key` changes whenever the player would have to be rebuilt for a clip.
 */
export function createPlayer(clip) {
  const frame = h('div', { class: 'player__frame' });
  const playBtn = h('button', { type: 'button', class: 'btn btn--icon', 'aria-label': 'Play clip', disabled: true }, icon('play', 16));
  const seek = h('input', { type: 'range', class: 'seek', min: '0', max: '1000', step: '1', value: '0', 'aria-label': 'Position in the clip', disabled: true });
  const time = h('span', { class: 'player__time' }, `0:00 / ${fmt.clock(clip.duration_s)}`);
  const el = h('div', { class: 'player' }, frame, h('div', { class: 'player__controls' }, playBtn, seek, time));

  let video = null;
  let raf = 0;
  let scrubbing = false;
  const timeListeners = [];

  const duration = () => (video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : Number(clip.duration_s) || 0);

  function paint() {
    if (!video) return;
    const d = duration();
    const t = Math.min(video.currentTime, d || video.currentTime);
    if (!scrubbing) {
      const p = d ? (t / d) * 1000 : 0;
      seek.value = String(Math.round(p));
      seek.style.setProperty('--p', `${p / 10}%`);
    }
    time.textContent = `${fmt.clock(t)} / ${fmt.clock(d)}`;
    for (const fn of timeListeners) fn(t);
  }

  function loop() {
    paint();
    raf = video && !video.paused && !video.ended ? requestAnimationFrame(loop) : 0;
  }

  function setPlayIcon() {
    const playing = video && !video.paused && !video.ended;
    replace(playBtn, icon(playing ? 'pause' : 'play', 16));
    playBtn.setAttribute('aria-label', playing ? 'Pause clip' : 'Play clip');
  }

  function showState(kind, labelText, detail, withHeadline) {
    replace(frame,
      withHeadline && clip.headline && !(clip.layout && clip.layout.show_headline === false)
        ? h('div', { class: 'ov-headline' }, h('span', null, clip.headline)) : null,
      h('div', { class: `player__state player__state--${kind}`, role: 'status' },
        h('span', { class: 'micro' }, labelText),
        kind === 'rendering' ? busyBar('Rendering') : null,
        detail ? h('span', { class: 'player__state-text' }, detail) : null));
  }

  function toggle() {
    if (!video) return;
    if (video.paused || video.ended) {
      const attempt = video.play();
      if (attempt && attempt.catch) attempt.catch(() => {});
    } else {
      video.pause();
    }
  }

  if (clip.render_state === 'rendering') {
    showState('rendering', 'Rendering', 'The clip is being rendered. It shows up here when it is done.', true);
  } else if (clip.render_state === 'failed') {
    showState('failed', 'Render failed', clip.render_error || 'No reason was given.', false);
  } else if (!clip.video_url) {
    showState('missing', 'No video', 'This clip has no rendered file.', false);
  } else {
    video = h('video', { class: 'player__video', src: clip.video_url, preload: 'metadata', playsinline: true });
    if (clip.thumb_url) video.poster = clip.thumb_url;
    video.addEventListener('click', toggle);
    video.addEventListener('loadedmetadata', paint);
    video.addEventListener('timeupdate', paint);
    video.addEventListener('seeked', paint);
    video.addEventListener('play', () => { setPlayIcon(); if (!raf) loop(); });
    video.addEventListener('pause', setPlayIcon);
    video.addEventListener('ended', setPlayIcon);
    video.addEventListener('error', () => {
      video = null;
      playBtn.disabled = true;
      seek.disabled = true;
      showState('failed', 'Cannot play', 'The video file could not be loaded.', false);
    });
    frame.append(video);
    playBtn.disabled = false;
    seek.disabled = false;
    playBtn.addEventListener('click', toggle);
    seek.addEventListener('input', () => {
      if (!video) return;
      scrubbing = true;
      const d = duration();
      seek.style.setProperty('--p', `${Number(seek.value) / 10}%`);
      if (d) video.currentTime = (Number(seek.value) / 1000) * d;
    });
    seek.addEventListener('change', () => { scrubbing = false; paint(); });
  }

  return {
    el,
    key: playerKey(clip),
    toggle,
    onTime(fn) { timeListeners.push(fn); },
    destroy() {
      cancelAnimationFrame(raf);
      if (video) {
        video.pause();
        video.removeAttribute('src');
        video.load();
        video = null;
      }
    },
  };
}

export function playerKey(clip) {
  return clip ? [clip.id, clip.render_state, clip.video_url ?? '', clip.render_error ?? ''].join('|') : '';
}
