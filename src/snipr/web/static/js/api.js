// The one place that talks to the server. Parses JSON, throws ApiError with the
// server's `detail` on any non-2xx answer, and treats 204 as "no body".

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function detailFrom(body, status) {
  const detail = body && typeof body === 'object' ? body.detail : null;
  if (typeof detail === 'string' && detail.trim()) return detail;
  // FastAPI's own validation errors arrive as a list of {loc, msg}.
  if (Array.isArray(detail) && detail.length) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item;
      if (!item || typeof item !== 'object') return '';
      const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : '';
      return where ? `${where}: ${item.msg}` : String(item.msg ?? '');
    }).filter(Boolean);
    if (parts.length) return parts.join(' ');
  }
  return `The request failed (${status}).`;
}

export async function request(method, path, body, options = {}) {
  const init = { method, headers: { Accept: 'application/json' }, signal: options.signal };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('Cannot reach Snipr. Check that it is still running.', 0);
  }
  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) throw new ApiError(detailFrom(data, res.status), res.status);
  return data;
}

const get = (path, options) => request('GET', path, undefined, options);
const post = (path, body, options) => request('POST', path, body, options);
const put = (path, body) => request('PUT', path, body);
const del = (path) => request('DELETE', path);

function query(params) {
  const q = new URLSearchParams();
  for (const [key, val] of Object.entries(params)) {
    if (val != null && val !== '') q.set(key, String(val));
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

export const api = {
  state: () => get('/api/state'),
  voices: () => get('/api/voices'),

  streamers: {
    list: () => get('/api/streamers'),
    resolve: (channelUrl, signal) => post('/api/streamers/resolve', { channel_url: channelUrl }, { signal }),
    sampleFrame: (channelUrl, games = '') => post('/api/streamers/sample-frame', { channel_url: channelUrl, games }),
    create: (body) => post('/api/streamers', body),
    update: (id, body) => put(`/api/streamers/${id}`, body),
    remove: (id) => del(`/api/streamers/${id}`),
    check: (id) => post(`/api/streamers/${id}/check`),
    testSelection: (id) => post(`/api/streamers/${id}/test-selection`),
  },

  clips: {
    list: (params = {}) => get(`/api/clips${query(params)}`),
    get: (id) => get(`/api/clips/${id}`),
    update: (id, body) => put(`/api/clips/${id}`, body),
    approve: (id, destinationIds, postNow) =>
      post(`/api/clips/${id}/approve`, { destination_ids: destinationIds, post_now: Boolean(postNow) }),
    reject: (id, reason) => post(`/api/clips/${id}/reject`, { reason: reason ?? null }),
    restore: (id) => post(`/api/clips/${id}/restore`),
    writeNarration: (id) => post(`/api/clips/${id}/write-narration`),
  },

  destinations: {
    list: () => get('/api/destinations'),
    create: (body) => post('/api/destinations', body),
    update: (id, body) => put(`/api/destinations/${id}`, body),
    remove: (id) => del(`/api/destinations/${id}`),
  },

  posts: {
    list: (status = 'open') => get(`/api/posts${query({ status })}`),
    postNow: (id) => post(`/api/posts/${id}/post-now`),
    markPosted: (id, postUrl) => post(`/api/posts/${id}/mark-posted`, { post_url: postUrl || null }),
    cancel: (id) => post(`/api/posts/${id}/cancel`),
    reveal: (id) => post(`/api/posts/${id}/reveal`),
  },

  activity: {
    get: () => get('/api/activity'),
    retryJob: (id) => post(`/api/jobs/${id}/retry`),
    runJobNow: (id) => post(`/api/jobs/${id}/run-now`),
    runNow: () => post('/api/run-now'),
    processVod: (url, streamerId) => post('/api/vods', { url, streamer_id: streamerId }),
  },

  settings: {
    get: () => get('/api/settings'),
    update: (body) => put('/api/settings', body),
    setKey: (key) => post('/api/settings/api-key', { key }),
    removeKey: () => del('/api/settings/api-key'),
  },
};
