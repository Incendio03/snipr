// Entry point: hash router, left navigation badges, state polling.

import { h, replace } from './dom.js';
import { getState, refreshState, startStatePolling, subscribe } from './state.js';
import { emptyBlock } from './ui.js';
import * as review from './views/review.js';
import * as clipEdit from './views/clip-edit.js';
import * as streamers from './views/streamers.js';
import * as streamerForm from './views/streamer-form.js';
import * as publishing from './views/publishing.js';
import * as activity from './views/activity.js';
import * as settings from './views/settings.js';

const ROUTES = [
  { pattern: /^\/review$/, nav: 'review', title: 'Review', view: review },
  { pattern: /^\/review\/(\d+)\/edit$/, nav: 'review', title: 'Edit clip', view: clipEdit },
  { pattern: /^\/streamers$/, nav: 'streamers', title: 'Streamers', view: streamers },
  { pattern: /^\/streamers\/new$/, nav: 'streamers', title: 'Add streamer', view: streamerForm },
  { pattern: /^\/streamers\/(\d+)$/, nav: 'streamers', title: 'Edit streamer', view: streamerForm },
  { pattern: /^\/publishing$/, nav: 'publishing', title: 'Publishing', view: publishing },
  { pattern: /^\/activity$/, nav: 'activity', title: 'Activity', view: activity },
  { pattern: /^\/settings$/, nav: 'settings', title: 'Settings', view: settings },
];

const main = document.getElementById('main');
let cleanup = null;
let firstRender = true;

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/review';
  const [path, queryString = ''] = raw.split('?');
  return { path: path.replace(/\/+$/, '') || '/review', query: new URLSearchParams(queryString) };
}

function render() {
  if (!location.hash || location.hash === '#' || location.hash === '#/') {
    history.replaceState(null, '', '#/review');
  }
  const { path, query } = parseHash();

  if (cleanup) {
    try { cleanup(); } catch (err) { console.error(err); }
    cleanup = null;
  }
  main.className = 'main';
  replace(main);

  let match = null;
  const route = ROUTES.find((r) => { match = r.pattern.exec(path); return match; });

  for (const link of document.querySelectorAll('[data-nav]')) {
    if (route && link.dataset.nav === route.nav) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }

  if (!route) {
    document.title = 'Snipr';
    main.append(emptyBlock('There is no page at this address', 'The link may be old or mistyped.',
      h('a', { class: 'btn', href: '#/review' }, 'Go to Review')));
  } else {
    document.title = `${route.title} · Snipr`;
    const ctx = {
      params: match.slice(1),
      query,
      state: getState,
      onState: subscribe,
      refreshState,
      navigate(hash) { location.hash = hash; },
    };
    try {
      cleanup = route.view.mount(main, ctx) ?? null;
    } catch (err) {
      console.error(err);
      replace(main, emptyBlock('This page hit an error', String(err && err.message ? err.message : err)));
    }
  }

  // Move focus to the new page for keyboard and screen reader users, but
  // leave it alone on first load.
  if (!firstRender) {
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }
  firstRender = false;
}

function setBadge(id, count, describe) {
  const el = document.getElementById(id);
  const n = Number(count) || 0;
  el.hidden = n <= 0;
  if (n > 0) {
    replace(el, h('span', { 'aria-hidden': 'true' }, String(n)), h('span', { class: 'sr-only' }, `, ${describe(n)}`));
  }
}

function paintNav(state, error) {
  const conn = document.getElementById('nav-conn');
  conn.hidden = !error;
  conn.textContent = error ? `${error.message} Trying again every few seconds.` : '';
  if (!state) return;
  setBadge('nav-review', state.review_count, (n) => `${n} waiting for review`);
  setBadge('nav-publishing', state.due_posts, (n) => `${n} due to post`);
  setBadge('nav-activity', state.failed_jobs, (n) => `${n} failed`);
  document.getElementById('nav-version').textContent = state.version ? `v${state.version}` : '';
}

subscribe(paintNav);
window.addEventListener('hashchange', render);
startStatePolling();
render();
