// Streamers list: one row per watched channel, with its settings at a glance,
// what it has produced, and a switch to pause it.

import { h, icon, replace } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import { toast, toastError, loadingBlock, errorBlock, emptyBlock, llmNotice, pageHead, toggle, withBusy } from '../ui.js';

export function mount(root, ctx) {
  let alive = true;
  let streamers = [];
  let dests = [];

  const head = pageHead({
    title: 'Streamers',
    sub: 'Each streamer has one source platform.',
    action: h('a', { class: 'btn btn--primary', href: '#/streamers/new', style: { gap: '8px' } }, icon('plus', 14), 'Add streamer'),
  });
  const noticeSlot = h('div', { hidden: true });
  const body = h('div');
  root.append(head.el, noticeSlot, body);

  function paintNotice(state) {
    const el = llmNotice(state);
    noticeSlot.hidden = !el;
    replace(noticeSlot, el);
  }
  const unsubscribe = ctx.onState(paintNotice);
  paintNotice(ctx.state());

  async function load() {
    replace(body, loadingBlock('Loading streamers'));
    try {
      const [list, destList] = await Promise.all([api.streamers.list(), api.destinations.list().catch(() => [])]);
      if (!alive) return;
      streamers = list ?? [];
      dests = destList ?? [];
      render();
    } catch (err) {
      if (alive) replace(body, errorBlock(err.message, load));
    }
  }

  function paintSub() {
    const on = streamers.filter((s) => s.enabled).length;
    const paused = streamers.length - on;
    head.setSub(streamers.length
      ? `${on} watched${paused ? `, ${paused} paused` : ''}. Each streamer has one source platform.`
      : 'Each streamer has one source platform.');
  }

  function render() {
    paintSub();
    if (!streamers.length) {
      replace(body, emptyBlock('No streamers yet',
        'Add a channel link and Snipr starts watching it for finished streams.',
        h('a', { class: 'btn', href: '#/streamers/new' }, 'Add streamer')));
      return;
    }
    replace(body, h('div', { class: 'table-wrap' },
      h('div', { class: 'streamers-table', role: 'table', 'aria-label': 'Streamers' },
        h('div', { class: 'table__head streamers-grid', role: 'row' },
          ['Streamer', 'Mode', 'Per VOD', 'Length', 'Layout', 'Default destination', 'Watching'].map((t) => h('span', { role: 'columnheader' }, t)),
          h('span', { role: 'columnheader' }, h('span', { class: 'sr-only' }, 'Edit'))),
        streamers.map(row))));
  }

  function cropNote(s) {
    if (s.layout === 'stacked') {
      return s.facecam_rect && s.gameplay_rect ? '' : 'Areas not marked';
    }
    return s.crop_rect ? '' : 'Centre crop, not marked';
  }

  function row(s) {
    const dest = dests.find((d) => d.id === s.default_destination_id);
    const stats = s.stats ?? {};
    const note = cropNote(s);
    const message = h('span', { class: 'streamer__msg', role: 'status' });
    const item = h('div', { class: `table__item streamer${s.enabled ? '' : ' streamer--paused'}` });

    const watching = toggle({
      pressed: s.enabled,
      label: (on) => (on ? 'On' : 'Paused'),
      onToggle: async (next, btn) => {
        btn.disabled = true;
        try {
          const updated = await api.streamers.update(s.id, { enabled: next });
          if (!alive) return;
          Object.assign(s, updated ?? { enabled: next });
        } catch (err) {
          if (!alive) return;
          toastError(err);
        } finally {
          if (alive) {
            btn.disabled = false;
            btn.setPressed(s.enabled);
            item.classList.toggle('streamer--paused', !s.enabled);
            paintSub();
          }
        }
      },
    });
    watching.setAttribute('aria-label', `Watching ${s.name}`);

    const checkBtn = h('button', { type: 'button', class: 'btn btn--text btn--small' }, 'Check now');
    checkBtn.addEventListener('click', async () => {
      message.className = 'streamer__msg';
      message.textContent = 'Looking for new streams';
      try {
        const result = await withBusy(checkBtn, () => api.streamers.check(s.id));
        if (!alive) return;
        message.textContent = (result && result.message) || 'Checked.';
        ctx.refreshState();
      } catch (err) {
        if (!alive) return;
        message.className = 'streamer__msg streamer__msg--error';
        message.textContent = err.message;
      }
    });

    const lastVod = stats.last_vod_title
      ? `Last stream: ${stats.last_vod_title}${stats.last_vod_at ? `, ${fmt.when(stats.last_vod_at)}` : ''}`
      : 'No stream processed yet';

    item.append(
      h('div', { class: 'table__row streamers-grid', role: 'row' },
        h('span', { class: 'cell-2', role: 'cell' },
          h('span', { class: 'strong' }, s.name),
          h('span', { class: 'mono small muted' }, fmt.channelShort(s.channel_url))),
        h('span', { role: 'cell' }, fmt.label(fmt.MODE, s.mode)),
        h('span', { class: 'num', role: 'cell' }, String(s.max_clips_per_vod)),
        h('span', { class: 'num', role: 'cell' }, `${s.clip_min_s} to ${s.clip_max_s} s`),
        h('span', { class: 'cell-2', role: 'cell' },
          h('span', null, fmt.label(fmt.LAYOUT, s.layout)),
          note ? h('span', { class: 'small', style: { color: 'var(--warn)' } }, note) : null),
        h('span', { role: 'cell' }, dest ? fmt.destName(dest) : 'None'),
        h('span', { role: 'cell' }, watching),
        h('span', { role: 'cell' }, h('a', { class: 'edit-link', href: `#/streamers/${s.id}`, 'aria-label': `Edit ${s.name}` }, 'Edit'))),
      h('div', { class: 'streamer__sub' },
        h('span', null, `${stats.pending ?? 0} waiting · ${stats.approved ?? 0} approved · ${stats.rejected ?? 0} rejected`),
        h('span', null, lastVod),
        h('span', null, stats.last_checked_at ? `Checked ${fmt.when(stats.last_checked_at)}` : 'Not checked yet'),
        s.games ? h('span', null, `Games: ${s.games}`) : null,
        message,
        checkBtn));
    return item;
  }

  // A toast set by the form after saving, shown once the list is on screen.
  const flash = ctx.query.get('saved');
  if (flash) {
    toast(flash === 'added' ? 'Streamer added. Snipr is watching for finished streams.' : flash === 'deleted' ? 'Streamer deleted.' : 'Saved.', { kind: 'ok' });
    history.replaceState(null, '', '#/streamers');
  }

  load();
  return () => { alive = false; unsubscribe(); };
}
