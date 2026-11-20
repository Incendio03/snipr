// Activity: the processing window, what the worker is doing, the job list,
// the event log, token usage, and a form to process one stream by link.

import { h, replace, keepFocus } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import { toast, toastError, loadingBlock, errorBlock, pageHead, field, errorLine, notice, withBusy, statusChip, busyBar } from '../ui.js';

const POLL_MS = 3000;
const RANK = { running: 0, failed: 1, waiting: 2, queued: 3, done: 4 };

function minutesOf(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm ?? ''));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

export function mount(root, ctx) {
  let alive = true;
  let data = null;
  let timer = null;
  let streamers = null;
  const busyJobs = new Set();
  let runAllBusy = false;
  let built = false;
  const painted = {}; // section name -> key of what is on screen

  root.classList.add('main--wide-gap');
  const head = pageHead({
    title: 'Activity',
    sub: 'New streams are found all day and listed here. The heavy work (downloading, transcription, encoding) starts when you press Run now, or by itself if you set a time window.',
  });
  const stale = h('div', { hidden: true, role: 'status' });
  const windowCard = h('section', { class: 'window-card', 'aria-label': 'Processing window' });
  const jobsBox = h('div');
  const eventsBox = h('div');
  const usageBox = h('div', { class: 'panel' });
  const body = h('div', { class: 'stack', style: { gap: '36px' } });
  root.append(head.el, stale, body);

  // ---------- Process one stream (built once, so typing is never reset) ----------
  const vodLink = h('input', { type: 'url', class: 'input', placeholder: 'https://www.twitch.tv/videos/123', autocomplete: 'off', spellcheck: 'false' });
  const vodStreamer = h('select', { class: 'input' }, h('option', { value: '' }, 'Loading streamers'));
  const vodError = errorLine();
  const vodDone = h('span', { class: 'help', role: 'status', style: { color: 'var(--ok)' } });
  const vodBtn = h('button', { type: 'submit', class: 'btn' }, 'Process this stream');
  const vodForm = h('form', { class: 'panel', novalidate: true, 'aria-label': 'Process one stream' },
    h('h2', { class: 'micro' }, 'Process one stream'),
    h('p', { class: 'muted', style: { fontSize: '13px' } }, 'Paste the link of a finished stream to run it through Snipr with a streamer\'s settings.'),
    field({ label: 'Stream link', control: vodLink }),
    field({ label: 'Streamer', control: vodStreamer }),
    vodError,
    h('div', { class: 'row row--tight' }, vodBtn, vodDone));
  vodForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    vodError.set('');
    vodDone.textContent = '';
    const url = vodLink.value.trim();
    if (!url) { vodError.set('Paste a stream link.'); vodLink.focus(); return; }
    if (!vodStreamer.value) { vodError.set(streamers && !streamers.length ? 'Add a streamer first.' : 'Choose a streamer.'); vodStreamer.focus(); return; }
    try {
      const result = await withBusy(vodBtn, () => api.activity.processVod(url, Number(vodStreamer.value)));
      if (!alive) return;
      vodDone.textContent = (result && result.message) || 'Queued.';
      vodLink.value = '';
      refresh();
    } catch (err) {
      if (alive) vodError.set(err.message);
    }
  });

  function loadStreamers() {
    api.streamers.list().then((list) => {
      if (!alive) return;
      streamers = list ?? [];
      replace(vodStreamer,
        h('option', { value: '' }, streamers.length ? 'Choose a streamer' : 'No streamers yet'),
        streamers.map((s) => h('option', { value: String(s.id) }, s.name)));
      if (streamers.length === 1) vodStreamer.value = String(streamers[0].id);
    }, (err) => {
      if (!alive) return;
      replace(vodStreamer, h('option', { value: '' }, 'Streamers did not load'));
      vodError.set(err.message);
    });
  }

  // ---------- Sections ----------
  function paint(name, key, render) {
    if (painted[name] === key) return;
    painted[name] = key;
    render();
  }

  function renderWindow() {
    const win = data.window ?? {};
    const worker = data.worker ?? {};
    const waiting = (data.jobs ?? []).filter((j) => j.status === 'waiting').length;
    const now = new Date();
    const key = JSON.stringify([win, worker, waiting, fmt.hm(now), runAllBusy]);
    paint('window', key, () => keepFocus(windowCard, () => {
      const start = minutesOf(win.start);
      const end = minutesOf(win.end);
      const segments = [];
      if (start != null && end != null) {
        if (end > start) segments.push([start, end]);
        else if (end < start) segments.push([start, 1440], [0, end]);
      }
      const nowMin = now.getHours() * 60 + now.getMinutes();

      const manual = win.mode !== 'window';
      const runAll = h('button', {
        type: 'button', class: `btn btn--ground${waiting ? ' btn--accent' : ''}`, 'data-key': 'run-all',
        disabled: runAllBusy || !waiting, title: waiting ? '' : 'No streams are waiting',
      }, waiting ? `Run now (${waiting})` : 'Run now');
      runAll.addEventListener('click', async () => {
        runAllBusy = true;
        renderWindow();
        try {
          const result = await api.activity.runNow();
          if (!alive) return;
          const n = result && typeof result.started === 'number' ? result.started : null;
          toast(n == null ? 'Started.' : n ? `Started ${fmt.plural(n, 'job')}.` : 'Nothing was waiting.', { kind: 'ok' });
        } catch (err) {
          if (alive) toastError(err);
        } finally {
          runAllBusy = false;
          if (alive) { renderWindow(); refresh(); }
        }
      });

      const workerLine = h('span', { class: 'worker' },
        h('span', { class: `dot${worker.busy ? ' dot--busy' : ''}`, 'aria-hidden': 'true' }),
        worker.busy ? `Worker: ${worker.current || 'busy'}` : 'Worker: idle');
      if (manual) {
        // Manual is the default: nothing heavy starts until Run now is pressed.
        replace(windowCard,
          h('div', { class: 'window-card__now' },
            h('h2', { class: 'micro' }, 'Processing'),
            h('span', { class: 'window-card__time' }, 'Manual'),
            h('span', { class: 'muted' }, waiting
              ? `${fmt.plural(waiting, 'stream')} waiting. Nothing starts until you press Run now.`
              : 'Nothing starts by itself. New streams wait here until you press Run now.'),
            workerLine),
          h('div', { class: 'window-card__bar' },
            h('span', { class: 'help' }, 'Want streams processed while you sleep? Set a time window in Settings and they will start by themselves inside it.')),
          h('div', { class: 'window-card__actions' },
            runAll,
            h('a', { class: 'btn btn--ground', href: '#/settings' }, 'Set a window')));
        return;
      }
      replace(windowCard,
        h('div', { class: 'window-card__now' },
          h('h2', { class: 'micro' }, 'Processing window'),
          h('span', { class: 'window-card__time' }, win.start && win.end ? `${win.start} to ${win.end}` : 'Not set'),
          h('span', { class: 'muted' }, win.open_now ? `Open now. Closes at ${win.end}.` : `Closed now. Opens at ${win.start}.`),
          workerLine),
        h('div', { class: 'window-card__bar' },
          h('div', { class: 'daybar', 'aria-hidden': 'true' },
            segments.map(([a, b]) => h('div', { class: 'daybar__open', style: { left: `${(a / 1440) * 100}%`, width: `${((b - a) / 1440) * 100}%` } })),
            h('div', { class: 'daybar__now', style: { left: `${(nowMin / 1440) * 100}%` } })),
          h('div', { class: 'daybar__ticks', 'aria-hidden': 'true' }, ['00', '06', '12', '18', '24'].map((t) => h('span', null, t))),
          h('span', { class: 'help' }, `The white line is the time now, ${fmt.hm(now)}.`)),
        h('div', { class: 'window-card__actions' },
          runAll,
          h('a', { class: 'btn btn--ground', href: '#/settings' }, 'Edit window')));
    }));
  }

  function jobNote(job, win) {
    const attempt = job.max_attempts ? `Attempt ${job.attempts ?? 1} of ${job.max_attempts}.` : '';
    if (job.status === 'failed') return { cls: 'job__note--danger', text: `${attempt} ${job.error || 'No reason was given.'}`.trim() };
    if (job.status === 'running' && (job.attempts ?? 1) > 1) return { cls: 'job__note--warn', text: `${attempt}${job.error ? ` Last try: ${job.error}` : ''}` };
    if (job.status === 'waiting') {
      if (!win || win.mode !== 'window') return { cls: '', text: 'Waiting for you to press Run now.' };
      return { cls: '', text: win.start ? `Waiting for the window at ${win.start}.` : 'Waiting for the processing window.' };
    }
    if (job.status === 'queued') return { cls: '', text: 'In line behind the current job.' };
    if (job.status === 'done') return { cls: '', text: job.updated_at ? `Finished ${fmt.when(job.updated_at)}.` : 'Finished.' };
    return { cls: '', text: job.updated_at ? `Updated ${fmt.when(job.updated_at)}.` : '' };
  }

  function jobRow(job) {
    const note = jobNote(job, data.window);
    const pct = typeof job.progress === 'number' ? Math.round(Math.min(1, Math.max(0, job.progress)) * 100) : null;
    const busy = busyJobs.has(job.id);
    let action = null;
    if (job.status === 'waiting') {
      action = h('button', { type: 'button', class: 'btn btn--accent', disabled: busy, 'data-key': `run-${job.id}`, onclick: () => jobAction(job, api.activity.runJobNow, 'Started without waiting for the window.') }, 'Run now');
    } else if (job.status === 'failed') {
      action = h('button', { type: 'button', class: 'btn', disabled: busy, 'data-key': `retry-${job.id}`, onclick: () => jobAction(job, api.activity.retryJob, 'Queued to run again.') }, 'Retry');
    }
    const retrying = job.status === 'running' && (job.attempts ?? 1) > 1;
    // Anything not running right now can be taken off the list. A cancelled stream is not picked up again.
    const cancel = ['waiting', 'queued', 'failed'].includes(job.status)
      ? h('button', { type: 'button', class: 'btn btn--text', disabled: busy, 'data-key': `cancel-${job.id}`, onclick: () => jobAction(job, api.activity.cancelJob, 'Cancelled. This stream will not be picked up again.') }, 'Cancel')
      : null;

    return h('div', { class: `table__item table__row jobs-grid job job--${job.status}`, role: 'row' },
      h('div', { class: 'cell-2', role: 'cell', style: { gap: '3px' } },
        h('span', { class: 'strong' }, job.label || `Job ${job.id}`),
        h('span', { class: 'muted' }, fmt.label(fmt.JOB_KIND, job.kind)),
        job.created_at ? h('span', { class: 'mono small faint' }, `Added ${fmt.when(job.created_at)}`) : null),
      h('div', { class: 'job__mid', role: 'cell' },
        h('div', { class: 'job__line' },
          retrying ? h('span', { class: 'status status--retrying' }, `Retry ${job.attempts} of ${job.max_attempts ?? '?'}`) : statusChip(job.status, fmt.JOB_STATUS),
          job.stage && job.status !== 'done' ? h('span', { class: 'job__stage' }, job.stage) : null,
          job.status === 'running' && pct != null ? h('span', { class: 'job__pct' }, `${pct}%`) : null),
        job.status === 'running'
          ? (pct != null
            ? h('div', { class: 'bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct), 'aria-label': `${job.stage || 'Progress'}` },
              h('div', { class: 'bar__fill', style: { width: `${pct}%` } }))
            : busyBar(job.stage || 'Working'))
          : null,
        note.text ? h('span', { class: `job__note ${note.cls}` }, note.text) : null),
      h('div', { role: 'cell', style: { display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: '8px' } }, cancel, action));
  }

  async function jobAction(job, call, doneText) {
    busyJobs.add(job.id);
    painted.jobs = null;
    renderJobs();
    try {
      const updated = await call(job.id);
      if (!alive) return;
      if (updated && updated.id === job.id) data.jobs = data.jobs.map((j) => (j.id === job.id ? updated : j));
      toast(doneText, { kind: 'ok' });
      ctx.refreshState();
    } catch (err) {
      if (alive) toastError(err);
    } finally {
      busyJobs.delete(job.id);
      if (alive) { painted.jobs = null; renderJobs(); refresh(); }
    }
  }

  function renderJobs() {
    const jobs = [...(data.jobs ?? [])].sort((a, b) => (RANK[a.status] ?? 9) - (RANK[b.status] ?? 9));
    const key = JSON.stringify([jobs, data.window, [...busyJobs]]);
    paint('jobs', key, () => keepFocus(jobsBox, () => {
      replace(jobsBox, h('section', { class: 'stack' },
        h('h2', { class: 'micro' }, 'Jobs'),
        jobs.length
          ? h('div', { class: 'table-wrap' }, h('div', { class: 'jobs-table', role: 'table', 'aria-label': 'Jobs' }, jobs.map(jobRow)))
          : h('p', { class: 'muted' }, 'No jobs yet. A job appears here when a watched streamer finishes a stream, or when you process one by link.')));
    }));
  }

  function renderEvents() {
    const events = data.events ?? [];
    paint('events', JSON.stringify(events), () => {
      const list = eventsBox.querySelector('.events');
      const scroll = list ? list.scrollTop : 0;
      replace(eventsBox, h('section', { class: 'stack' },
        h('h2', { class: 'micro' }, 'Event log'),
        events.length
          ? h('div', { class: 'events', tabindex: '0', role: 'log', 'aria-label': 'Event log, newest first' },
            events.map((ev) => h('div', { class: `event event--${ev.level}` },
              h('span', { class: 'event__at' }, fmt.when(ev.at)),
              h('span', { class: 'event__level' }, ev.level === 'error' ? 'Error' : ev.level === 'warn' ? 'Warn' : 'Info'),
              h('span', { class: 'event__msg' }, ev.message))))
          : h('p', { class: 'muted' }, 'Nothing has happened yet.')));
      const next = eventsBox.querySelector('.events');
      if (next) next.scrollTop = scroll;
    });
  }

  function renderUsage() {
    const usage = data.usage ?? {};
    paint('usage', JSON.stringify(usage), () => {
      const stat = (value, text) => h('div', { class: 'stat' }, h('span', { class: 'stat__num' }, fmt.int(value)), h('span', { class: 'stat__label' }, text));
      replace(usageBox,
        h('h2', { class: 'micro' }, `Token usage${usage.since ? ` · since ${fmt.day(usage.since)}` : ''}`),
        h('div', { class: 'stats' }, stat(usage.tokens_in, 'tokens in'), stat(usage.tokens_out, 'tokens out'), stat(usage.requests, 'requests')));
    });
  }

  function renderAll() {
    if (!built) {
      built = true;
      replace(body, windowCard, jobsBox,
        h('div', { class: 'activity-cols' }, eventsBox, h('div', { class: 'side' }, vodForm, usageBox)));
    }
    renderWindow();
    renderJobs();
    renderEvents();
    renderUsage();
  }

  // ---------- Polling ----------
  async function refresh() {
    clearTimeout(timer);
    try {
      const next = await api.activity.get();
      if (!alive) return;
      data = next ?? {};
      stale.hidden = true;
      renderAll();
    } catch (err) {
      if (!alive) return;
      if (!data) {
        replace(body, errorBlock(err.message, () => { replace(body, loadingBlock('Loading activity')); refresh(); }));
      } else {
        stale.hidden = false;
        replace(stale, notice('warn', `This page is not updating: ${err.message}`));
      }
    }
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    if (!alive) return;
    timer = setTimeout(() => {
      if (document.hidden) schedule(); else refresh();
    }, POLL_MS);
  }

  function onVisible() {
    if (!document.hidden && alive) refresh();
  }
  document.addEventListener('visibilitychange', onVisible);

  replace(body, loadingBlock('Loading activity'));
  loadStreamers();
  refresh();

  return () => {
    alive = false;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', onVisible);
  };
}
