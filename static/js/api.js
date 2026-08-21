/** Thin wrapper over the backend JSON API. */

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function readError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const detail = payload && payload.detail;
  let message;
  if (typeof detail === 'string') message = detail;
  else if (detail && typeof detail.message === 'string') message = detail.message;
  else message = `Request failed (HTTP ${response.status})`;
  return new ApiError(message, response.status, detail);
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (cause) {
    throw new ApiError('Could not reach the viewer server.', 0, String(cause));
  }
  if (!response.ok) throw await readError(response);
  return response.json();
}

const json = (method, body) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

export const api = {
  ApiError,

  config: () => request('/api/config'),

  entries: () => request('/api/entries'),

  entry: (stem) => request(`/api/entries/${encodeURIComponent(stem)}`),

  /** Where the entry's audio is served from, wherever it turned out to live. */
  audioUrl: (stem) => `/api/entries/${encodeURIComponent(stem)}/audio`,

  /** Overwrite one entry's annotation. The audio is never part of this. */
  updateEntry(stem, payload) {
    return request(`/api/entries/${encodeURIComponent(stem)}`, json('PUT', payload));
  },

  /** File an uploaded pair as a new entry; the audio is copied in. */
  createEntry({ file, payload, clientId }) {
    const body = new FormData();
    body.append('payload', JSON.stringify(payload));
    body.append('file', file);
    if (clientId) body.append('clientId', clientId);
    return request('/api/entries', { method: 'POST', body });
  },

  removeEntry(stem) {
    return request(`/api/entries/${encodeURIComponent(stem)}`, { method: 'DELETE' });
  },

  /** Read an uploaded .txt into segments, and hear about lines that would not. */
  parse(file) {
    const body = new FormData();
    body.append('file', file);
    return request('/api/parse', { method: 'POST', body });
  },
};
