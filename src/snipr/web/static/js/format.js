// Labels and number / time formatting shared by every screen.

export const PLATFORM = { twitch: 'Twitch', youtube: 'YouTube', kick: 'Kick', tiktok: 'TikTok', facebook: 'Facebook' };
export const MODE = { highlights: 'Highlights', informative: 'Informative', custom: 'Custom' };
export const LAYOUT = { fill: 'Fill frame', stacked: 'Stacked' };
export const FLAG = { slurs: 'Slurs', sexual: 'Sexual content', music: 'Copyrighted music' };
export const FLAG_SHORT = { slurs: 'Slurs', sexual: 'Sexual', music: 'Music' };
export const REJECT_REASONS = [
  ['boring', 'Boring'],
  ['bad_cut', 'Bad cut'],
  ['wrong_topic', 'Wrong topic'],
  ['bad_framing', 'Bad framing'],
  ['other', 'Other'],
];
export const POST_STATUS = { scheduled: 'Scheduled', due: 'Due', posting: 'Posting', posted: 'Posted', failed: 'Failed' };
export const JOB_STATUS = { queued: 'Queued', waiting: 'Waiting', running: 'Running', failed: 'Failed', done: 'Done' };
export const JOB_KIND = { process_vod: 'Process stream', render_clip: 'Render clip' };
export const METHOD = { export: 'Export', api: 'API' };

/** Look a key up in a label map, falling back to the raw key. */
export function label(map, key) {
  if (key == null) return '';
  return map[key] ?? String(key);
}

export function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

const pad = (n) => String(n).padStart(2, '0');

/** 41 -> "0:41", 3725 -> "1:02:05". For clip lengths and player time. */
export function clock(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  const hrs = Math.floor(s / 3600);
  const min = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return hrs ? `${hrs}:${pad(min)}:${pad(sec)}` : `${min}:${pad(sec)}`;
}

/** 8048 -> "02:14:08", 8048.5 -> "02:14:08.5". For positions in a stream. */
export function timecode(seconds) {
  const tenths = Math.max(0, Math.round((Number(seconds) || 0) * 10));
  const whole = Math.floor(tenths / 10);
  const frac = tenths % 10;
  const hrs = Math.floor(whole / 3600);
  const min = Math.floor((whole % 3600) / 60);
  const sec = whole % 60;
  return `${pad(hrs)}:${pad(min)}:${pad(sec)}${frac ? `.${frac}` : ''}`;
}

/** Accepts "02:14:08", "2:14:08.5", "14:08" or plain seconds. Returns null when unreadable. */
export function parseTimecode(text) {
  const str = String(text ?? '').trim().replace(',', '.');
  if (!str) return null;
  const parts = str.split(':');
  if (parts.length > 3) return null;
  if (!parts.every((p, i) => (i === parts.length - 1 ? /^\d+(\.\d+)?$/ : /^\d+$/).test(p))) return null;
  const nums = parts.map(Number);
  if (parts.length > 1 && nums.slice(1).some((n) => n >= 60)) return null;
  let total = 0;
  for (const n of nums) total = total * 60 + n;
  return Math.round(total * 10) / 10;
}

/** 41 -> "41 s", 41.5 -> "41.5 s". */
export function secs(seconds) {
  const n = Math.round((Number(seconds) || 0) * 10) / 10;
  return `${Number.isInteger(n) ? n : n.toFixed(1)} s`;
}

export function score(value) {
  const n = Number(value);
  return value == null || Number.isNaN(n) ? '–' : n.toFixed(2);
}

export function int(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString('en-US') : '–';
}

export function gb(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '–';
  return `${n >= 100 ? Math.round(n) : n.toFixed(1)} GB`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** The API sends local time without an offset, which Date parses as local. */
export function parseDate(iso) {
  if (!iso) return null;
  const d = iso instanceof Date ? iso : new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function hm(date) {
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** "19 Nov", with the year when it is not this year. */
export function day(iso) {
  const d = parseDate(iso);
  if (!d) return '';
  const base = `${d.getDate()} ${MONTHS[d.getMonth()]}`;
  return d.getFullYear() === new Date().getFullYear() ? base : `${base} ${d.getFullYear()}`;
}

function relativeDay(d) {
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(d) - start(new Date())) / 86400000);
  if (diff === 0) return 'today';
  if (diff === 1) return 'tomorrow';
  if (diff === -1) return 'yesterday';
  return day(d);
}

/** "tomorrow 12:00", "today 18:00", "24 Nov 12:00". Empty string for null. */
export function when(iso) {
  const d = parseDate(iso);
  return d ? `${relativeDay(d)} ${hm(d)}` : '';
}

/** "https://www.twitch.tv/name/" -> "twitch.tv/name" */
export function channelShort(url) {
  return String(url ?? '').replace(/^https?:\/\//i, '').replace(/^www\./i, '').replace(/\/+$/, '');
}

export function destName(dest) {
  return `${dest.name} · ${label(PLATFORM, dest.platform)}`;
}

export function hashtagsText(tags) {
  return (tags ?? []).map((t) => `#${String(t).replace(/^#+/, '')}`).join(' ');
}

/** "#gta6, gaming  #clips" -> ["gta6", "gaming", "clips"] */
export function parseHashtags(text) {
  const seen = new Set();
  const out = [];
  for (const raw of String(text ?? '').split(/[\s,]+/)) {
    const tag = raw.replace(/^#+/, '').trim();
    if (tag && !seen.has(tag.toLowerCase())) {
      seen.add(tag.toLowerCase());
      out.push(tag);
    }
  }
  return out;
}

export function wordCount(text) {
  return String(text ?? '').trim().split(/\s+/).filter(Boolean).length;
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;
export function isHHMM(text) {
  return HHMM.test(String(text ?? '').trim());
}
/** "9:5" is not accepted, "9:05" becomes "09:05". */
export function normaliseHHMM(text) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(text ?? '').trim());
  if (!m) return null;
  const out = `${pad(Number(m[1]))}:${m[2]}`;
  return HHMM.test(out) ? out : null;
}
