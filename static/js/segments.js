/**
 * The editable segment table.
 *
 * Rows are rebuilt only when the *set* of rows changes (load, add, delete,
 * re-sort). While the user is typing, the model and the derived cells are
 * patched in place -- rebuilding on every keystroke would steal focus and the
 * caret. Re-sorting is therefore deferred to `change` (blur / Enter) rather
 * than firing on `input`.
 */

import {
  cleanSpeaker,
  cleanText,
  formatDuration,
  formatTime,
  parseTime,
  sortSegments,
  speakerColor,
  timeFieldValue,
  uniqueSpeakers,
} from './annotation.js';

const PLAY_ICON = '<svg class="icon-play" viewBox="0 0 24 24" aria-hidden="true">' +
  '<path d="M8 5v14l11-7z"/></svg>';
const PAUSE_ICON = '<svg class="icon-pause" viewBox="0 0 24 24" aria-hidden="true" hidden>' +
  '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/></svg>';

export class SegmentTable {
  constructor({ tbody, emptyEl, countEl, textHeadEl, onChange, onSeek, onSelect,
                onStructureChange, onPlay }) {
    this.tbody = tbody;
    this.emptyEl = emptyEl;
    this.countEl = countEl;
    this.textHeadEl = textHeadEl;

    this.onChange = onChange || (() => {});
    this.onSeek = onSeek || (() => {});
    this.onSelect = onSelect || (() => {});
    this.onStructureChange = onStructureChange || (() => {});
    this.onPlay = onPlay || (() => {});

    this.segments = [];
    this.errors = new Map();
    this.sortMode = 'time';
    this.milliseconds = true;
    this.selectedId = null;
    this.playingId = null;
    this.isPlaying = false;
    this.serviceHasTranscript = false;

    this._bind();
  }

  /* ---------------------------------------------------------------- state */

  setData(segments, { serviceHasTranscript = false } = {}) {
    this.segments = segments;
    this.serviceHasTranscript = serviceHasTranscript;
    this.render();
  }

  setSortMode(mode) {
    this.sortMode = mode;
    this.render();
  }

  setMilliseconds(enabled) {
    this.milliseconds = enabled;
    this.render();
  }

  setErrors(errors) {
    this.errors = errors;
    this._paintErrors();
  }

  setSelected(id) {
    this.selectedId = id;
    for (const row of this.tbody.rows) {
      row.classList.toggle('is-selected', row.dataset.id === id);
    }
  }

  /** Which row the playhead is inside, and whether the audio is actually moving. */
  setPlaying(id, isPlaying = this.isPlaying) {
    if (id === this.playingId && isPlaying === this.isPlaying) return;
    this.playingId = id;
    this.isPlaying = isPlaying;
    for (const row of this.tbody.rows) {
      const here = row.dataset.id === id;
      row.classList.toggle('is-playing', here);
      // The button shows what pressing it would do: pause only where sound is
      // actually coming from right now.
      const button = row.querySelector('[data-action="play"]');
      if (!button) continue;
      const sounding = here && isPlaying;
      // Attribute, not property: these are SVG elements, and `hidden` as a
      // property is an HTMLElement thing that would silently do nothing.
      button.querySelector('.icon-play').toggleAttribute('hidden', sounding);
      button.querySelector('.icon-pause').toggleAttribute('hidden', !sounding);
      button.title = sounding ? 'Pause' : (button.dataset.playTitle || 'Play this segment');
    }
  }

  /** Rows in the order currently displayed. */
  get ordered() {
    return sortSegments(this.segments, this.sortMode);
  }

  /* --------------------------------------------------------------- render */

  render() {
    const ordered = this.ordered;
    const speakers = uniqueSpeakers(this.segments);

    this.tbody.innerHTML = '';
    for (const segment of ordered) {
      this.tbody.appendChild(this._row(segment, speakers));
    }

    this.emptyEl.hidden = ordered.length > 0;
    this.countEl.textContent = ordered.length
      ? `${ordered.length} segment${ordered.length === 1 ? '' : 's'} · ${speakers.length} speaker${speakers.length === 1 ? '' : 's'}`
      : '';

    if (this.textHeadEl) {
      this.textHeadEl.textContent = this.serviceHasTranscript
        ? 'Transcript'
        : 'Transcript (none returned)';
      this.textHeadEl.title = this.serviceHasTranscript
        ? 'Transcript returned by DiseService — editable'
        : 'DiseService returned no transcript for this audio; type one here';
    }

    this.setSelected(this.selectedId);
    this.setPlaying(this.playingId);
    this._paintErrors();
  }

  _row(segment, speakers) {
    const row = document.createElement('tr');
    row.dataset.id = segment.id;

    const play = document.createElement('td');
    const at = formatTime(segment.start, this.milliseconds);
    play.innerHTML =
      `<button type="button" class="jump-btn" data-action="play" ` +
      `data-play-title="Play this segment (from ${at})" ` +
      `title="Play this segment (from ${at})" ` +
      `aria-label="Play the segment at ${at}">${PLAY_ICON}${PAUSE_ICON}</button>`;
    row.appendChild(play);

    const speaker = document.createElement('td');
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = speakerColor(segment.speaker);
    const speakerInput = input('cell-input speaker', segment.speaker, 'speaker');
    speakerInput.setAttribute('list', 'speakerOptions');
    speakerInput.setAttribute('aria-label', 'Speaker');
    const speakerCell = document.createElement('div');
    speakerCell.className = 'speaker-cell';
    speakerCell.append(swatch, speakerInput);
    speaker.appendChild(speakerCell);
    speaker.appendChild(errorSlot());
    row.appendChild(speaker);

    for (const field of ['start', 'end']) {
      const cell = document.createElement('td');
      const box = input('cell-input time', timeFieldValue(segment, field, this.milliseconds), field);
      box.setAttribute('inputmode', 'numeric');
      box.setAttribute('spellcheck', 'false');
      box.setAttribute('aria-label', field === 'start' ? 'Start time' : 'End time');
      box.placeholder = this.milliseconds ? 'hh:mm:ss.mmm' : 'hh:mm:ss';
      cell.appendChild(box);
      cell.appendChild(errorSlot());
      row.appendChild(cell);
    }

    const duration = document.createElement('td');
    duration.className = 'dur-cell';
    duration.dataset.role = 'duration';
    duration.textContent = durationText(segment);
    row.appendChild(duration);

    const text = document.createElement('td');
    const textInput = input('cell-input', segment.text, 'text');
    textInput.placeholder = 'Transcription…';
    textInput.setAttribute('aria-label', 'Transcript');
    text.appendChild(textInput);
    row.appendChild(text);

    const actions = document.createElement('td');
    actions.innerHTML =
      '<button type="button" class="btn icon ghost danger" data-action="delete" ' +
      'title="Delete segment" aria-label="Delete segment">✕</button>';
    row.appendChild(actions);

    // A single shared datalist keeps existing speaker names one keystroke away.
    if (!document.getElementById('speakerOptions')) {
      const list = document.createElement('datalist');
      list.id = 'speakerOptions';
      document.body.appendChild(list);
    }
    const list = document.getElementById('speakerOptions');
    const wanted = speakers.map((name) => `<option value="${name.replace(/"/g, '&quot;')}">`).join('');
    if (list.innerHTML !== wanted) list.innerHTML = wanted;

    return row;
  }

  _paintErrors() {
    for (const row of this.tbody.rows) {
      const entry = this.errors.get(row.dataset.id) || {};
      row.classList.toggle('has-error', Object.keys(entry).length > 0);

      for (const box of row.querySelectorAll('[data-field]')) {
        const message = entry[box.dataset.field];
        box.classList.toggle('is-invalid', Boolean(message));
        box.setAttribute('aria-invalid', message ? 'true' : 'false');
        const slot = box.closest('td').querySelector('[data-role="error"]');
        if (slot) slot.textContent = message || '';
      }
    }
  }

  _find(id) {
    return this.segments.find((segment) => segment.id === id);
  }

  /* --------------------------------------------------------------- events */

  _bind() {
    // Typing: update the model and derived cells, but never re-render.
    this.tbody.addEventListener('input', (event) => {
      const box = event.target.closest('[data-field]');
      if (!box) return;
      const segment = this._find(box.closest('tr').dataset.id);
      if (!segment) return;

      this._apply(segment, box);

      const durationCell = box.closest('tr').querySelector('[data-role="duration"]');
      if (durationCell) durationCell.textContent = durationText(segment);
      if (box.dataset.field === 'speaker') {
        const swatch = box.closest('tr').querySelector('.swatch');
        if (swatch) swatch.style.background = speakerColor(segment.speaker);
      }
      this.onChange({ reorder: false });
    });

    // Committing an edit (blur or Enter): normalise the text and re-sort.
    this.tbody.addEventListener('change', (event) => {
      const box = event.target.closest('[data-field]');
      if (!box) return;
      const segment = this._find(box.closest('tr').dataset.id);
      if (!segment) return;

      this._apply(segment, box);
      if (box.dataset.field === 'start' || box.dataset.field === 'end') {
        if (segment[box.dataset.field] !== null) {
          box.value = formatTime(segment[box.dataset.field], this.milliseconds);
        }
      } else if (box.dataset.field === 'speaker') {
        box.value = segment.speaker;
      }
      this.onChange({ reorder: true });
    });

    this.tbody.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && event.target.matches('[data-field]')) {
        event.preventDefault();
        event.target.blur();
      }
    });

    this.tbody.addEventListener('click', (event) => {
      const row = event.target.closest('tr');
      if (!row) return;
      const segment = this._find(row.dataset.id);
      if (!segment) return;

      const action = event.target.closest('[data-action]')?.dataset.action;
      if (action === 'play') {
        this.onSelect(segment.id);
        this.onPlay(segment.id);
        return;
      }
      if (action === 'delete') {
        // Splice in place rather than rebinding: the app holds the same array,
        // and a fresh one here would leave the deleted segment alive over there
        // -- still validated, still scored, still saved.
        const index = this.segments.findIndex((item) => item.id === segment.id);
        if (index >= 0) this.segments.splice(index, 1);
        if (this.selectedId === segment.id) this.selectedId = null;
        this.render();
        this.onStructureChange(this.segments);
        this.onChange({ reorder: false });
        return;
      }
      // Clicking the row chrome (not a field) selects without stealing focus.
      if (!event.target.closest('[data-field]')) this.onSelect(segment.id);
    });

    this.tbody.addEventListener('focusin', (event) => {
      const row = event.target.closest('tr');
      if (row) this.onSelect(row.dataset.id);
    });
  }

  _apply(segment, box) {
    const field = box.dataset.field;
    if (field === 'speaker') {
      segment.speaker = cleanSpeaker(box.value);
    } else if (field === 'text') {
      segment.text = cleanText(box.value);
    } else {
      // null marks "unparseable", which validation reports as a format error.
      const parsed = parseTime(box.value);
      segment[field] = parsed;
      segment[`${field}Raw`] = parsed === null ? box.value : null;
    }
  }

  /** Focus a row's first field, scrolling it into view. */
  focusSegment(id) {
    const row = [...this.tbody.rows].find((item) => item.dataset.id === id);
    if (!row) return;
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    row.querySelector('[data-field="speaker"]')?.focus();
  }

  /** Rewrite the time boxes after an external edit (e.g. a waveform drag). */
  syncTimes(id) {
    const segment = this._find(id);
    const row = [...this.tbody.rows].find((item) => item.dataset.id === id);
    if (!segment || !row) return;
    for (const field of ['start', 'end']) {
      const box = row.querySelector(`[data-field="${field}"]`);
      if (box && document.activeElement !== box) {
        box.value = timeFieldValue(segment, field, this.milliseconds);
      }
    }
    const durationCell = row.querySelector('[data-role="duration"]');
    if (durationCell) durationCell.textContent = durationText(segment);
  }
}

/* ------------------------------------------------------------------ helpers */

function input(className, value, field) {
  const box = document.createElement('input');
  box.type = 'text';
  box.className = className;
  box.value = value ?? '';
  box.dataset.field = field;
  return box;
}

function errorSlot() {
  const slot = document.createElement('span');
  slot.className = 'field-error';
  slot.dataset.role = 'error';
  return slot;
}

function durationText(segment) {
  if (segment.start === null || segment.end === null) return '—';
  const value = segment.end - segment.start;
  return value > 0 ? formatDuration(value) : '—';
}
