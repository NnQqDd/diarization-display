/**
 * Who is speaking: one row per distinct speaker in the annotation.
 *
 * The rows are derived from the segments -- a speaker exists because a segment
 * carries its label -- so labels are renamed in the segment table, not here.
 * What lives here is what the segments cannot say: gender and language, both
 * free text and both entirely optional, exactly like the transcript. Nothing
 * typed here can block a save.
 *
 * Values are kept per label rather than per row, so re-deriving the list (after
 * an edit, a delete, a re-sort) does not lose what has been typed. A label that
 * disappears keeps its entry in memory in case it comes back from a typo, and
 * is simply not saved.
 */

import { formatDuration, speakerColor, uniqueSpeakers } from './annotation.js';

const FIELDS = ['gender', 'language'];

export class SpeakerTable {
  constructor({ tbody, emptyEl, countEl, onChange }) {
    this.tbody = tbody;
    this.emptyEl = emptyEl;
    this.countEl = countEl;
    this.onChange = onChange || (() => {});

    /** label -> { gender, language }, kept across re-renders. */
    this.profiles = new Map();
    this.labels = [];
    this.stats = new Map();

    this._bind();
  }

  _bind() {
    this.tbody.addEventListener('input', (event) => {
      const box = event.target.closest('[data-field]');
      if (!box) return;
      const label = box.closest('tr')?.dataset.speaker;
      if (!label) return;
      const entry = this.profiles.get(label) || {};
      entry[box.dataset.field] = box.value;
      this.profiles.set(label, entry);
      this.onChange();
    });
  }

  /** Re-derive the rows from the annotation. */
  setData(segments) {
    const labels = uniqueSpeakers(segments);
    this.stats = statsBySpeaker(segments);

    const same = labels.length === this.labels.length
      && labels.every((label, index) => label === this.labels[index]);
    this.labels = labels;

    // Rebuilding while someone is typing in one of these cells would take the
    // caret with it, so the rows are only rebuilt when the *set* changes.
    if (same) this._patch();
    else this.render();
  }

  /** `[{ speaker, gender, language }]` for the speakers that currently exist. */
  getProfiles() {
    return this.labels.map((label) => {
      const entry = this.profiles.get(label) || {};
      return {
        speaker: label,
        gender: (entry.gender || '').trim(),
        language: (entry.language || '').trim(),
      };
    });
  }

  /** Adopt saved values, e.g. when an annotation is reloaded. */
  setProfiles(profiles) {
    this.profiles = new Map();
    for (const item of profiles || []) {
      if (!item || !item.speaker) continue;
      this.profiles.set(item.speaker, {
        gender: item.gender || '',
        language: item.language || '',
      });
    }
    this.render();
  }

  clear() {
    this.profiles = new Map();
    this.labels = [];
    this.stats = new Map();
    this.render();
  }

  render() {
    this.tbody.innerHTML = '';
    for (const label of this.labels) this.tbody.appendChild(this._row(label));

    this.emptyEl.hidden = this.labels.length > 0;
    this.countEl.textContent = this.labels.length
      ? `${this.labels.length} speaker${this.labels.length === 1 ? '' : 's'}`
      : '';
  }

  _patch() {
    for (const row of this.tbody.rows) {
      const stat = this.stats.get(row.dataset.speaker);
      const cell = row.querySelector('[data-role="share"]');
      if (cell && stat) cell.textContent = shareText(stat);
    }
  }

  _row(label) {
    const row = document.createElement('tr');
    row.dataset.speaker = label;

    const name = document.createElement('td');
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = speakerColor(label);
    const text = document.createElement('span');
    text.className = 'speaker-name';
    text.textContent = label;
    const cell = document.createElement('div');
    cell.className = 'speaker-cell';
    cell.append(swatch, text);
    name.appendChild(cell);
    row.appendChild(name);

    const share = document.createElement('td');
    share.className = 'dur-cell';
    share.dataset.role = 'share';
    share.textContent = shareText(this.stats.get(label));
    row.appendChild(share);

    const entry = this.profiles.get(label) || {};
    for (const field of FIELDS) {
      const td = document.createElement('td');
      const box = document.createElement('input');
      box.type = 'text';
      box.className = 'cell-input';
      box.dataset.field = field;
      box.value = entry[field] || '';
      box.placeholder = 'Optional…';
      box.setAttribute('aria-label', `${label} ${field}`);
      box.setAttribute('list', `${field}Options`);
      td.appendChild(box);
      row.appendChild(td);
    }

    return row;
  }
}

function statsBySpeaker(segments) {
  const stats = new Map();
  for (const segment of segments) {
    if (!segment.speaker) continue;
    const entry = stats.get(segment.speaker) || { count: 0, seconds: 0 };
    entry.count += 1;
    if (segment.start !== null && segment.end !== null && segment.end > segment.start) {
      entry.seconds += segment.end - segment.start;
    }
    stats.set(segment.speaker, entry);
  }
  return stats;
}

function shareText(stat) {
  if (!stat) return '—';
  return `${stat.count} segment${stat.count === 1 ? '' : 's'} · ${formatDuration(stat.seconds)}`;
}
