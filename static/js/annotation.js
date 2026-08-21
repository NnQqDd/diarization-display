/**
 * Segment model, hh:mm:ss handling, validation, sorting and speaker colours.
 *
 * These rules mirror app/annotation.py so the annotator gets instant feedback
 * as they type. The server re-runs the same checks before anything is saved --
 * this module is for responsiveness, not for trust.
 */

// hh:mm:ss with an optional fractional part (used by "ms precision" mode).
const TIME_RE = /^(\d{1,3}):([0-5]?\d):([0-5]?\d)(?:\.(\d{1,6}))?$/;

// Two times closer than this count as equal -- far below the 1 ms display grid.
export const EPSILON = 1e-6;

let nextId = 1;

export function makeSegment({ speaker, start, end, text = '' }) {
  // `startRaw`/`endRaw` hold the literal text of an unparseable edit, so a
  // re-render can show the annotator what they typed instead of overwriting it
  // with a formatted stand-in for null.
  return { id: `s${nextId++}`, speaker, start, end, text, startRaw: null, endRaw: null };
}

/** What a time field should display: the raw text if it did not parse. */
export function timeFieldValue(segment, field, milliseconds) {
  if (segment[field] === null || segment[field] === undefined) {
    return segment[`${field}Raw`] ?? '';
  }
  return formatTime(segment[field], milliseconds);
}

/** Parse "hh:mm:ss" / "hh:mm:ss.mmm" into seconds. Returns null when invalid. */
export function parseTime(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value !== 'string') return null;

  const text = value.trim();
  const match = TIME_RE.exec(text);
  if (!match) return null;

  const [, hh, mm, ss, frac] = match;
  let total = Number(hh) * 3600 + Number(mm) * 60 + Number(ss);
  if (frac) total += Number(frac) / 10 ** frac.length;
  return total;
}

/** Render seconds as "hh:mm:ss", or "hh:mm:ss.mmm" when `milliseconds`. */
export function formatTime(seconds, milliseconds = false) {
  const value = Math.max(0, Number(seconds) || 0);
  let whole;
  let tail = '';
  if (milliseconds) {
    const totalMs = Math.round(value * 1000);
    whole = Math.floor(totalMs / 1000);
    tail = `.${String(totalMs % 1000).padStart(3, '0')}`;
  } else {
    whole = Math.round(value);
  }
  const hh = Math.floor(whole / 3600);
  const mm = Math.floor((whole % 3600) / 60);
  const ss = whole % 60;
  return (
    `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}:` +
    `${String(ss).padStart(2, '0')}${tail}`
  );
}

/** Compact duration for the table, e.g. "4.0s" or "1:23.5". */
export function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${value.toFixed(1)}s`;
  const mins = Math.floor(value / 60);
  return `${mins}:${(value - mins * 60).toFixed(1).padStart(4, '0')}`;
}

/** Speaker labels are space-free: the dataset file is space-delimited. */
export function cleanSpeaker(value) {
  return String(value ?? '').trim().split(/\s+/).filter(Boolean).join('_');
}

export function cleanText(value) {
  return String(value ?? '').replace(/\s+/g, ' ').trim();
}

/**
 * Validate every segment against `audioStart <= start < end <= audioEnd`.
 *
 * Returns a Map of segment id -> { speaker?, start?, end? } messages, so all
 * bad rows can be marked at once instead of stopping at the first. Two
 * different speakers overlapping is allowed -- it is legitimate diarization
 * output. One speaker overlapping itself is not: nobody talks over themselves,
 * and DER counts the doubled seconds twice. Mirrors app/annotation.py.
 */
export function validateSegments(segments, audioStart, audioEnd, milliseconds = true) {
  const errors = new Map();
  // Quote bounds back at the precision being edited, or the message names a
  // different bound from the one actually enforced.
  const bound = (value) => formatTime(value, milliseconds);

  const add = (segment, field, message) => {
    const entry = errors.get(segment.id) || {};
    if (!entry[field]) entry[field] = message;
    errors.set(segment.id, entry);
  };

  // A time that did not parse is either blank or malformed, and the two want
  // different advice: "this is required" vs "this is the wrong shape".
  const wasLeftBlank = (segment, field) => {
    const raw = segment[`${field}Raw`];
    return raw === null || raw === undefined || String(raw).trim() === '';
  };

  for (const segment of segments) {
    if (!segment.speaker) add(segment, 'speaker', 'Speaker is required');

    if (segment.start === null) {
      add(segment, 'start',
          wasLeftBlank(segment, 'start') ? 'Start is required' : 'Use hh:mm:ss');
    } else if (segment.start < audioStart - EPSILON) {
      add(segment, 'start', `Before audio start (${bound(audioStart)})`);
    } else if (segment.start > audioEnd + EPSILON) {
      add(segment, 'start', `Past audio end (${bound(audioEnd)})`);
    }

    if (segment.end === null) {
      add(segment, 'end',
          wasLeftBlank(segment, 'end') ? 'End is required' : 'Use hh:mm:ss');
    } else if (segment.end > audioEnd + EPSILON) {
      add(segment, 'end', `Past audio end (${bound(audioEnd)})`);
    } else if (segment.end < audioStart - EPSILON) {
      add(segment, 'end', `Before audio start (${bound(audioStart)})`);
    }

    if (
      segment.start !== null && segment.end !== null &&
      segment.end - segment.start <= EPSILON
    ) {
      add(segment, 'end', 'End must be after start');
    }
  }

  // Per speaker, sweep in start order against the furthest end seen so far, so
  // one long segment covering several short ones flags every one of them.
  // Rows already flagged as empty or inverted are left out -- they would only
  // gain a second red field. Touching boundaries are not an overlap.
  const bySpeaker = new Map();
  for (const segment of segments) {
    if (!segment.speaker || segment.start === null || segment.end === null) continue;
    if (segment.end - segment.start <= EPSILON) continue;
    const list = bySpeaker.get(segment.speaker);
    if (list) list.push(segment);
    else bySpeaker.set(segment.speaker, [segment]);
  }
  for (const [speaker, list] of bySpeaker) {
    list.sort((a, b) => a.start - b.start || a.end - b.end);
    let reach = -Infinity;
    for (const segment of list) {
      if (segment.start < reach - EPSILON) {
        add(segment, 'start', `${speaker} speaks until ${bound(reach)}`);
      }
      reach = Math.max(reach, segment.end);
    }
  }

  return errors;
}

/** Order `speaker_2` before `speaker_10` by comparing the trailing number. */
function speakerKey(label) {
  const match = /(\d+)\s*$/.exec(label || '');
  return match
    ? [0, Number(match[1]), (label || '').toLowerCase()]
    : [1, 0, (label || '').toLowerCase()];
}

function compare(a, b) {
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

/** "time" = chronological; "speaker" = grouped by speaker, then chronological. */
export function sortSegments(segments, mode = 'time') {
  const copy = [...segments];
  if (mode === 'speaker') {
    copy.sort((a, b) =>
      compare(speakerKey(a.speaker), speakerKey(b.speaker)) ||
      (a.start ?? 0) - (b.start ?? 0) ||
      (a.end ?? 0) - (b.end ?? 0));
  } else {
    copy.sort((a, b) =>
      (a.start ?? 0) - (b.start ?? 0) ||
      (a.end ?? 0) - (b.end ?? 0) ||
      compare(speakerKey(a.speaker), speakerKey(b.speaker)));
  }
  return copy;
}

export function uniqueSpeakers(segments) {
  const seen = [...new Set(segments.map((s) => s.speaker).filter(Boolean))];
  seen.sort((a, b) => compare(speakerKey(a), speakerKey(b)));
  return seen;
}

/** Round onto the editing grid, keeping the segment non-empty and in bounds. */
export function snapTime(seconds, grid, audioStart, audioEnd) {
  if (seconds === null || !Number.isFinite(seconds)) return seconds;
  const snapped = Math.round(seconds / grid) * grid;
  return Math.min(Math.max(snapped, audioStart), audioEnd);
}

// Distinct hues that stay legible on the dark waveform and in the table.
const SPEAKER_COLORS = [
  '#4c9aff', '#f778ba', '#3fb950', '#d29922', '#a371f7',
  '#2dd4bf', '#ff7b72', '#7ee787', '#ffa657', '#79c0ff',
  '#e879f9', '#facc15',
];

const colorCache = new Map();

/** Stable colour per speaker: assigned in first-seen order, then memoised. */
export function speakerColor(label) {
  const key = label || '';
  if (!colorCache.has(key)) {
    colorCache.set(key, SPEAKER_COLORS[colorCache.size % SPEAKER_COLORS.length]);
  }
  return colorCache.get(key);
}

export function resetSpeakerColors(labels = []) {
  colorCache.clear();
  labels.forEach((label) => speakerColor(label));
}
