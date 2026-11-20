// Publishing: destinations with their posting slots, the posts waiting to go
// out (due ones first, with everything needed to upload by hand), and what was
// posted recently.

import { h, icon, replace, keepFocus, isTypingTarget } from '../dom.js';
import { api } from '../api.js';
import * as fmt from '../format.js';
import {
  toast, toastError, loadingBlock, errorBlock, emptyBlock, pageHead, field, errorLine, toggle, notice,
  withBusy, slotsEditor, statusChip, thumb, copyButton,
} from '../ui.js';

const PLATFORMS = ['youtube', 'tiktok', 'facebook'];

export function mount(root, ctx) {
  let alive = true;
  let dests = null;
  let destsError = null;
  let openPosts = null;
  let postedPosts = [];
  let postsError = null;
  let defaultSlots = ['12:00', '18:00'];
  let editingDest = null;      // null | 'new' | destination id (full editor)
  let editingSlots = null;     // destination id whose slots are being edited
  let confirmingDelete = null; // destination id
  let confirmingCancel = null; // post id
  let seenDue = null;
  const expanded = new Set();  // post ids with the details panel open
  const links = new Map();     // post id -> text typed into "Post link"
  const cardErrors = new Map(); // destination id -> message

  root.classList.add('main--wide-gap');
  const head = pageHead({ title: 'Publishing', sub: "An approved clip waits for its destination's next free posting slot." });
  const destSection = h('section', { class: 'stack' });
  const postSection = h('div', { class: 'stack', style: { gap: '36px' } });
  root.append(head.el, destSection, postSection);

  // ---------- Loading ----------
  async function loadDests() {
    try {
      dests = await api.destinations.list();
      destsError = null;
    } catch (err) {
      destsError = err;
    }
    // Do not redraw the cards under an open editor; it redraws when it closes.
    const editorOpen = dests && (editingDest != null || editingSlots != null);
    if (alive && !editorOpen) renderDests();
  }

  async function loadPosts() {
    try {
      const [open, posted] = await Promise.all([api.posts.list('open'), api.posts.list('posted')]);
      openPosts = open ?? [];
      postedPosts = posted ?? [];
      postsError = null;
      const state = ctx.state();
      if (state) seenDue = state.due_posts;
    } catch (err) {
      postsError = err;
    }
    if (alive) renderPosts();
  }

  function reloadAll() {
    ctx.refreshState();
    return Promise.all([loadDests(), loadPosts()]);
  }

  // ---------- Destinations ----------
  function renderDests() {
    keepFocus(destSection, () => {
      const addBtn = h('button', {
        type: 'button', class: 'btn', style: { gap: '8px' }, 'data-key': 'add-dest', disabled: editingDest === 'new',
        onclick: () => { editingDest = 'new'; editingSlots = null; renderDests(); focusFirst('[data-editor] input'); },
      }, icon('plus', 14), 'Add destination');
      const headRow = h('div', { class: 'section-head' }, h('h2', { class: 'micro' }, 'Destinations'), addBtn);

      if (destsError && !dests) {
        replace(destSection, headRow, errorBlock(destsError.message, loadDests));
        return;
      }
      if (!dests) {
        replace(destSection, headRow, loadingBlock('Loading destinations'));
        return;
      }
      const cards = dests.map((d) => (editingDest === d.id ? destEditor(d) : destCard(d)));
      if (editingDest === 'new') cards.push(destEditor(null));
      replace(destSection, headRow,
        destsError ? notice('warn', `Destinations could not be refreshed: ${destsError.message}`) : null,
        cards.length
          ? h('div', { class: 'dest-grid' }, cards)
          : emptyBlock('No destinations yet', 'A destination is one of your accounts, for example "Funny clips" on YouTube. Approved clips are queued for the destinations you tick in review.'));
    });
  }

  function focusFirst(selector) {
    const el = destSection.querySelector(selector);
    if (el) el.focus();
  }

  function destCard(d) {
    const error = cardErrors.get(d.id);
    const card = h('div', { class: `card${d.enabled === false ? ' card--paused' : ''}` },
      h('div', { class: 'cell-2' },
        h('span', { class: 'card__title' }, d.name),
        h('span', { class: 'card__sub' }, fmt.label(fmt.PLATFORM, d.platform), ' ',
          d.account_handle ? h('span', { class: 'mono' }, d.account_handle) : null,
          d.enabled === false ? ' · paused' : null)));

    // Slots
    if (editingSlots === d.id) {
      const editor = slotsEditor(d.post_slots ?? [], { ground: true });
      const save = h('button', { type: 'button', class: 'btn btn--small', 'data-key': `slots-save-${d.id}` }, 'Save slots');
      save.addEventListener('click', async () => {
        const slots = editor.read();
        if (!slots) return;
        await updateDest(d, { post_slots: slots }, save, () => { editingSlots = null; });
      });
      card.append(h('div', { class: 'block' },
        h('span', { class: 'label' }, 'Slots, 24 hour local time'),
        editor,
        h('div', { class: 'row row--tight' }, save,
          h('button', { type: 'button', class: 'btn btn--text btn--small', onclick: () => { editingSlots = null; renderDests(); } }, 'Cancel'))));
    } else {
      card.append(h('div', { class: 'slots' },
        h('span', { class: 'label', style: { marginRight: '4px' } }, 'Slots'),
        (d.post_slots ?? []).map((s) => h('span', { class: 'slot-chip' }, s)),
        h('button', {
          type: 'button', class: 'btn btn--text', 'data-key': `slots-${d.id}`, 'aria-label': `Edit slots for ${fmt.destName(d)}`,
          onclick: () => { editingSlots = d.id; editingDest = null; renderDests(); focusFirst('.slots-edit input'); },
        }, 'Edit slots')));
    }

    // Posting method
    const method = (key, text) => {
      const on = d.post_method === key;
      const blocked = key === 'api' && !d.api_available;
      const btn = h('button', { type: 'button', class: 'btn', 'aria-pressed': String(on), disabled: blocked, 'data-key': `method-${key}-${d.id}` }, text);
      btn.addEventListener('click', () => { if (!on) updateDest(d, { post_method: key }, btn); });
      return btn;
    };
    const methodNote = d.post_method === 'api'
      ? 'Snipr uploads at each slot.'
      : `You get a reminder at each slot and upload by hand.${d.api_available ? ' API upload is available for this account.' : d.api_note ? ` ${d.api_note}` : ''}`;
    card.append(h('div', { class: 'block', style: { gap: '6px' } },
      h('span', { class: 'label' }, 'Posting method'),
      h('div', { class: 'seg', role: 'group', 'aria-label': 'Posting method' }, method('export', 'Export'), method('api', 'API upload')),
      h('span', { class: 'help' }, methodNote)));

    card.append(
      h('div', { class: 'card__line' }, h('span', null, 'Next free slot'), h('span', { class: 'mono' }, fmt.when(d.next_free_slot) || 'none')),
      h('div', { class: 'card__line' }, h('span', null, 'Queued'), h('span', { class: 'mono' }, fmt.plural(d.queued ?? 0, 'post'))));

    if (error) card.append(h('div', { role: 'alert' }, notice('danger', error)));

    // Edit / delete
    const foot = h('div', { class: 'card__foot' });
    if (confirmingDelete === d.id) {
      const yes = h('button', { type: 'button', class: 'btn btn--danger btn--small', 'data-key': `del-yes-${d.id}` }, 'Delete');
      const no = h('button', { type: 'button', class: 'btn btn--small', 'data-key': `del-no-${d.id}`, onclick: () => { confirmingDelete = null; renderDests(); } }, 'Keep');
      yes.addEventListener('click', async () => {
        cardErrors.delete(d.id);
        try {
          await withBusy([yes, no], () => api.destinations.remove(d.id));
          if (!alive) return;
          confirmingDelete = null;
          toast(`Deleted ${fmt.destName(d)}.`, { kind: 'ok' });
          await loadDests();
        } catch (err) {
          if (!alive) return;
          confirmingDelete = null;
          cardErrors.set(d.id, err.message);
          renderDests();
        }
      });
      foot.append(h('span', { class: 'confirm__text', role: 'alert' }, 'Delete this destination?'), yes, no);
    } else {
      foot.append(
        h('button', {
          type: 'button', class: 'btn btn--small', 'data-key': `edit-${d.id}`,
          onclick: () => { editingDest = d.id; editingSlots = null; confirmingDelete = null; renderDests(); focusFirst('[data-editor] input'); },
        }, 'Edit'),
        h('button', {
          type: 'button', class: 'btn btn--text btn--small', style: { color: 'var(--danger)' }, 'data-key': `del-${d.id}`,
          onclick: () => { confirmingDelete = d.id; cardErrors.delete(d.id); renderDests(); focusFirst(`[data-key="del-no-${d.id}"]`); },
        }, 'Delete'));
    }
    card.append(foot);
    return card;
  }

  async function updateDest(d, patch, button, onDone) {
    cardErrors.delete(d.id);
    try {
      const updated = await withBusy(button, () => api.destinations.update(d.id, patch));
      if (!alive) return;
      if (updated) dests = dests.map((x) => (x.id === d.id ? updated : x));
      if (onDone) onDone();
      renderDests();
      loadDests();
    } catch (err) {
      if (!alive) return;
      cardErrors.set(d.id, err.message);
      renderDests();
    }
  }

  function destEditor(d) {
    const isNew = !d;
    const name = h('input', { type: 'text', class: 'input', value: d ? d.name : '', placeholder: 'Funny clips', autocomplete: 'off' });
    const platform = h('select', { class: 'input' }, PLATFORMS.map((p) => h('option', { value: p }, fmt.PLATFORM[p])));
    platform.value = d ? d.platform : 'youtube';
    const handle = h('input', { type: 'text', class: 'input input--mono', value: d ? d.account_handle ?? '' : '', placeholder: '@account', autocomplete: 'off', spellcheck: 'false' });
    const slots = slotsEditor(d ? d.post_slots ?? [] : defaultSlots, { ground: true });
    const enabled = toggle({ pressed: d ? d.enabled !== false : true, label: (on) => (on ? 'In use' : 'Paused') });
    const error = errorLine();
    const save = h('button', { type: 'submit', class: 'btn btn--primary' }, isNew ? 'Add destination' : 'Save');

    const form = h('form', { class: 'card', novalidate: true, dataset: { editor: '1' }, 'aria-label': isNew ? 'New destination' : `Edit ${d.name}` },
      h('span', { class: 'card__title' }, isNew ? 'New destination' : `Edit ${d.name}`),
      field({ label: 'Name', control: name }),
      field({ label: 'Platform', control: platform }),
      field({ label: 'Account handle', control: handle, optional: true }),
      h('div', { class: 'block', style: { gap: '6px' } }, h('span', { class: 'label' }, 'Slots, 24 hour local time'), slots),
      isNew ? h('span', { class: 'help' }, 'A new destination starts on Export: you upload by hand and mark the post as posted.') : null,
      enabled,
      error,
      h('div', { class: 'card__foot' }, save,
        h('button', { type: 'button', class: 'btn btn--small', onclick: () => { editingDest = null; renderDests(); } }, 'Cancel')));

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      error.set('');
      if (!name.value.trim()) { error.set('Give the destination a name.'); name.focus(); return; }
      const slotList = slots.read();
      if (!slotList) return;
      const data = {
        name: name.value.trim(), platform: platform.value, account_handle: handle.value.trim(),
        post_slots: slotList, enabled: enabled.isPressed(),
      };
      if (isNew) data.post_method = 'export';
      try {
        await withBusy(save, () => (isNew ? api.destinations.create(data) : api.destinations.update(d.id, data)));
        if (!alive) return;
        editingDest = null;
        toast(isNew ? `Added ${data.name}.` : 'Saved.', { kind: 'ok' });
        await loadDests();
      } catch (err) {
        if (alive) error.set(err.message);
      }
    });
    return form;
  }

  // ---------- Posts ----------
  const destOf = (p) => `${p.destination_name} · ${fmt.label(fmt.PLATFORM, p.platform)}`;

  function renderPosts() {
    keepFocus(postSection, () => {
      if (postsError && !openPosts) {
        replace(postSection, h('section', { class: 'stack' }, h('h2', { class: 'micro' }, 'Posts'), errorBlock(postsError.message, loadPosts)));
        return;
      }
      if (!openPosts) {
        replace(postSection, h('section', { class: 'stack' }, h('h2', { class: 'micro' }, 'Posts'), loadingBlock('Loading posts')));
        return;
      }
      const due = openPosts.filter((p) => p.status === 'due');
      const rest = openPosts.filter((p) => p.status !== 'due');
      const out = [];
      if (postsError) out.push(notice('warn', `The posts below could not be refreshed: ${postsError.message}`));

      if (due.length) {
        out.push(h('section', { class: 'stack', 'aria-label': 'Due now' },
          h('h2', { class: 'micro micro--accent' }, `Due now · ${fmt.plural(due.length, 'post')} waiting for your upload`),
          due.map((p) => postPanel(p, false))));
      }

      out.push(h('section', { class: 'stack' },
        h('h2', { class: 'micro' }, 'Waiting for a slot · one status per clip and destination'),
        rest.length
          ? h('div', { class: 'table-wrap' },
            h('div', { class: 'posts-table', role: 'table', 'aria-label': 'Posts waiting for a slot' },
              h('div', { class: 'table__head posts-grid', role: 'row' },
                ['Clip', 'Destination', 'Method', 'Status', 'Slot'].map((t) => h('span', { role: 'columnheader' }, t)),
                h('span', { role: 'columnheader' }, h('span', { class: 'sr-only' }, 'Actions'))),
              rest.map(openRow)))
          : emptyBlock(due.length ? 'Nothing else is scheduled' : 'Nothing is waiting to be posted',
            'Approve a clip in Review and it is queued here for the next free slot of each destination you ticked.',
            h('a', { class: 'btn', href: '#/review' }, 'Open Review'))));

      out.push(h('section', { class: 'stack' },
        h('h2', { class: 'micro' }, 'Recently posted'),
        postedPosts.length
          ? h('div', { class: 'table-wrap' },
            h('div', { class: 'posts-table', role: 'table', 'aria-label': 'Recently posted' },
              h('div', { class: 'table__head posted-grid', role: 'row' },
                ['Clip', 'Destination', 'Method', 'Status', 'Posted'].map((t) => h('span', { role: 'columnheader' }, t)),
                h('span', { role: 'columnheader' }, h('span', { class: 'sr-only' }, 'Link'))),
              postedPosts.map(postedRow)))
          : h('p', { class: 'muted' }, 'Nothing has been posted yet.')));

      replace(postSection, out);
    });
  }

  function clipCell(p) {
    const clip = p.clip ?? {};
    return h('span', { class: 'clip-cell', role: 'cell' },
      thumb(clip.thumb_url, 'small'),
      h('span', { class: 'cell-2' },
        h('span', { class: 'strong' }, clip.headline || clip.title || `Clip ${p.clip_id}`),
        h('span', { class: 'small muted' }, clip.streamer_name ?? '')));
  }

  function statusCell(p) {
    const detail = p.status === 'failed' ? (p.error || 'The upload failed.')
      : p.status === 'posting' ? 'Uploading'
        : p.status === 'due' ? 'Waiting for your upload' : '';
    return h('span', { role: 'cell', style: { display: 'flex', flexDirection: 'column', gap: '4px', alignItems: 'flex-start', minWidth: '0' } },
      statusChip(p.status, fmt.POST_STATUS),
      detail ? h('span', { class: 'small muted', style: { overflowWrap: 'anywhere' } }, detail) : null);
  }

  function openRow(p) {
    const open = expanded.has(p.id);
    const actions = h('span', { class: 'row-actions', role: 'cell' });
    if (p.status === 'scheduled') {
      const now = h('button', { type: 'button', class: 'btn btn--small', 'data-key': `now-${p.id}` }, 'Post now');
      now.addEventListener('click', () => postAction(now, () => api.posts.postNow(p.id), p.method === 'export' ? 'Due now. Upload it and mark it as posted.' : 'Posting now.'));
      actions.append(now);
    }
    actions.append(h('button', {
      type: 'button', class: 'btn btn--small btn--quiet', 'aria-expanded': String(open), 'data-key': `details-${p.id}`,
      onclick: () => { if (open) expanded.delete(p.id); else expanded.add(p.id); renderPosts(); },
    }, open ? 'Close' : 'Details'));

    return h('div', { class: 'table__item' },
      h('div', { class: `table__row posts-grid${open ? ' table__row--open' : ''}`, role: 'row' },
        clipCell(p),
        h('span', { role: 'cell' }, destOf(p)),
        h('span', { class: 'muted', role: 'cell' }, fmt.label(fmt.METHOD, p.method)),
        statusCell(p),
        h('span', { class: 'num muted', role: 'cell' }, fmt.when(p.scheduled_at) || '–'),
        actions),
      open ? h('div', { class: 'table__detail' }, postPanel(p, true)) : null);
  }

  function postedRow(p) {
    return h('div', { class: 'table__item' },
      h('div', { class: 'table__row posted-grid', role: 'row' },
        clipCell(p),
        h('span', { role: 'cell' }, destOf(p)),
        h('span', { class: 'muted', role: 'cell' }, fmt.label(fmt.METHOD, p.method)),
        statusCell(p),
        h('span', { class: 'num muted', role: 'cell' }, fmt.when(p.posted_at) || '–'),
        h('span', { class: 'row-actions', role: 'cell' },
          p.post_url && /^https?:\/\//i.test(p.post_url)
            ? h('a', { class: 'btn btn--small', href: p.post_url, target: '_blank', rel: 'noopener noreferrer' }, 'View post')
            : h('span', { class: 'small faint' }, 'No link saved'))));
  }

  /** Everything needed to upload one post by hand. */
  function postPanel(p, inline) {
    const clip = p.clip ?? {};
    const linkInput = h('input', {
      type: 'url', class: 'input input--ground', id: `p-link-${p.id}`, 'data-key': `link-${p.id}`,
      placeholder: 'Paste the link once the post is live', value: links.get(p.id) ?? '', autocomplete: 'off',
    });
    linkInput.addEventListener('input', () => links.set(p.id, linkInput.value));

    const copyCard = (labelText, text, tags) => h('div', { class: 'copy-card' },
      h('div', { class: 'copy-card__head' }, h('span', null, labelText), text ? copyButton(() => text, labelText.toLowerCase()) : null),
      h('p', { class: `copy-card__text${tags ? ' copy-card__text--tags' : ''}` }, text || '–'));

    const reveal = h('button', { type: 'button', class: 'btn', 'data-key': `reveal-${p.id}` }, 'Show file');
    reveal.addEventListener('click', async () => {
      try {
        await withBusy(reveal, () => api.posts.reveal(p.id));
      } catch (err) {
        if (alive) toastError(err);
      }
    });

    const mark = h('button', { type: 'button', class: 'btn btn--primary', 'data-key': `mark-${p.id}` }, 'Mark as posted');
    mark.addEventListener('click', () => {
      const url = linkInput.value.trim();
      if (url && !/^https?:\/\/\S+$/i.test(url)) {
        toast('The post link has to start with http:// or https://. Leave it empty if you do not have one.', { kind: 'error' });
        linkInput.focus();
        return;
      }
      postAction(mark, () => api.posts.markPosted(p.id, url || null), 'Marked as posted.', () => { links.delete(p.id); expanded.delete(p.id); });
    });

    const cancelSlot = h('span', { class: 'confirm' });
    if (confirmingCancel === p.id) {
      const yes = h('button', { type: 'button', class: 'btn btn--danger', 'data-key': `cancel-yes-${p.id}` }, 'Remove post');
      yes.addEventListener('click', () => postAction(yes, () => api.posts.cancel(p.id), 'Post removed. The clip stays approved.', () => { confirmingCancel = null; expanded.delete(p.id); }));
      cancelSlot.append(yes, h('button', { type: 'button', class: 'btn', 'data-key': `cancel-no-${p.id}`, onclick: () => { confirmingCancel = null; renderPosts(); } }, 'Keep'));
    } else {
      cancelSlot.append(h('button', {
        type: 'button', class: 'btn btn--quiet', 'data-key': `cancel-${p.id}`,
        onclick: () => {
          confirmingCancel = p.id;
          renderPosts();
          const keep = postSection.querySelector(`[data-key="cancel-no-${p.id}"]`);
          if (keep) keep.focus();
        },
      }, 'Cancel post'));
    }

    const slot = fmt.when(p.scheduled_at);
    const sub = p.status === 'due'
      ? `For ${destOf(p)}${slot ? `, slot ${slot}` : ''}. Upload the file by hand, then mark it as posted.`
      : `For ${destOf(p)}${slot ? `, slot ${slot}` : ''}.`;

    return h('section', { class: `export${inline ? ' export--inline' : ''}`, 'aria-label': `Export for manual upload: ${clip.headline ?? ''}` },
      h('div', { class: 'export__head' },
        thumb(clip.thumb_url, 'large'),
        h('div', { class: 'export__headtext' },
          inline ? null : h('span', { class: 'micro' }, 'Export for manual upload'),
          h('h3', { class: 'export__title' }, clip.headline || clip.title || `Clip ${p.clip_id}`),
          h('span', { class: 'export__sub' }, sub),
          h('span', { class: 'small muted' }, [clip.streamer_name, clip.duration_s != null ? fmt.clock(clip.duration_s) : null].filter(Boolean).join(' · ')),
          h('div', { class: 'export__file', style: { marginTop: '8px' } },
            h('span', { class: 'label' }, 'Clip file'),
            h('span', { class: 'export__path' }, clip.file_path || 'No file path'),
            reveal))),
      h('div', { class: 'copy-grid' },
        copyCard('Title', clip.title),
        copyCard('Description', clip.post_text),
        copyCard('Hashtags', clip.hashtags_text, true)),
      h('div', { class: 'export__foot' },
        h('div', { class: 'field' },
          h('label', { class: 'label', for: linkInput.id }, 'Post link', h('span', { class: 'label__opt' }, ' · optional')),
          linkInput),
        mark, cancelSlot));
  }

  async function postAction(button, action, doneText, onDone) {
    try {
      await withBusy(button, action);
      if (!alive) return;
      if (onDone) onDone();
      toast(doneText, { kind: 'ok' });
      await reloadAll();
    } catch (err) {
      if (alive) toastError(err);
    }
  }

  // A slot coming due changes the list: reload quietly, unless the user is
  // typing or has an editor open.
  const unsubscribe = ctx.onState((state) => {
    if (!alive || !state || openPosts == null) return;
    if (seenDue == null) { seenDue = state.due_posts; return; }
    if (state.due_posts === seenDue) return;
    if (isTypingTarget(document.activeElement) && root.contains(document.activeElement)) return;
    seenDue = state.due_posts;
    loadPosts();
    if (editingDest == null && editingSlots == null) loadDests();
  });

  renderDests();
  renderPosts();
  api.settings.get().then((s) => {
    const slots = s && s.publishing && s.publishing.default_post_slots;
    if (Array.isArray(slots) && slots.length) defaultSlots = slots;
  }, () => {});
  loadDests();
  loadPosts();

  return () => { alive = false; unsubscribe(); };
}
