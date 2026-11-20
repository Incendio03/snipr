// App-wide state from GET /api/state, polled every 5 seconds. Views subscribe
// to it for the nav badges, the LLM notice and "something changed" hints.

import { api } from './api.js';

const POLL_MS = 5000;

let data = null;
let error = null;
let timer = null;
let inFlight = null;
const listeners = new Set();

export function getState() {
  return data;
}

export function getStateError() {
  return error;
}

/** Call fn(state, error) on every update. Returns the unsubscribe function. */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit() {
  for (const fn of [...listeners]) {
    try { fn(data, error); } catch (err) { console.error(err); }
  }
}

/** Fetch now. Safe to call often: concurrent calls share one request. */
export function refreshState() {
  if (inFlight) return inFlight;
  inFlight = api.state()
    .then((next) => { data = next; error = null; })
    .catch((err) => { error = err; })
    .finally(() => { inFlight = null; emit(); });
  return inFlight;
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    if (!document.hidden) await refreshState();
    schedule();
  }, POLL_MS);
}

export function startStatePolling() {
  refreshState();
  schedule();
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshState();
  });
}
