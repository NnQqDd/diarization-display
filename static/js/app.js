/**
 * Wiring: pick an entry -> waveform + editable table -> save it back.
 *
 * The shape to keep in mind is that an entry is an *annotation*, and the audio
 * is something this app goes looking for. It may be beside the annotation, it
 * may be on the shelf the annotation UI hands files out from, and it may be
 * gone -- in which case everything except the waveform still works, because
 * the times in the file are the work, not the sound.
 *
 * `state.original` is the annotation as it was read from disk. Every edit is
 * measured against it, so "Save changes" and "Revert" both know whether there
 * is anything to do.
 */

import {
  formatTime,
  makeSegment,
  snapTime,
  sortSegments,
  speakerColor,
  uniqueSpeakers,
  validateSegments,
} from './annotation.js';
import { api } from './api.js';
import { clientId } from './identity.js';
import { SegmentTable } from './segments.js';
import { SpeakerTable } from './speakers.js';
import { Waveform } from './waveform.js';

const $ = (id) => document.getElementById(id);

const state = {
  // --- the browser -------------------------------------------------------
  entries: [],
  entrySort: 'savedAt',
  entryFilter: '',

  // --- what is open ------------------------------------------------------
  // The dataset stem when an entry from the list is open; null for a pair
  // that has been uploaded but not filed.
  stem: null,
  // 'dataset' or 'pair'. Saving means two different things, so this decides
  // which button the footer offers.
  source: null,
  meta: {},
  audioSource: 'missing',
  // The uploaded audio, held for the waveform, the player, and the eventual
  // upload if the pair is filed.
  pairFile: null,
  objectUrl: null,

  // --- the annotation ----------------------------------------------------
  segments: [],
  // A snapshot of the same, taken at load: what Revert goes back to, and what
  // "unsaved changes" is measured against.
  original: '',
  duration: 0,
  annotationEnd: 0,
  selectedId: null,
  playingSegmentId: null,
  grid: 0.001,
  milliseconds: true,
  errors: new Map(),
  loading: false,
  // Where the audio of the entry that was just closed had been, so the delete
  // toast can say whether anything was left behind on the shelf.
  audioSourceWas: null,
};

let waveform = null;
let table = null;
let speakers = null;

/* ------------------------------------------------------------------ startup */

function init() {
  waveform = new Waveform({
    scrollEl: $('waveScroll'),
    spacerEl: $('waveSpacer'),
    canvasEl: $('waveCanvas'),
    rulerEl: $('waveRuler'),
    onSeek: (time) => seek(time),
    onSelect: (id) => select(id),
    onEdgeDrag: (id, side, time, commit) => dragEdge(id, side, time, commit),
  });

  speakers = new SpeakerTable({
    tbody: $('speakersBody'),
    emptyEl: $('speakersEmpty'),
    countEl: $('speakerCount'),
    onChange: () => markDirty(),
  });

  table = new SegmentTable({
    tbody: $('segmentsBody'),
    emptyEl: $('segmentsEmpty'),
    countEl: $('segmentCount'),
    onChange: ({ reorder }) => onAnnotationChanged({ reorder }),
    onSeek: (time) => seek(time),
    onSelect: (id) => select(id),
    onPlay: (id) => playSegment(id),
    onStructureChange: (segments) => {
      if (segments) state.segments = segments;
      syncWaveform();
    },
  });

  bindBrowser();
  bindPair();
  bindPlayer();
  bindToolbar();
  loadConfig();
  loadEntries();

  // Leaving with unsaved edits is a real loss: the annotation on disk is
  // somebody's finished work and the edits exist nowhere else.
  window.addEventListener('beforeunload', (event) => {
    if (!isDirty()) return;
    event.preventDefault();
    event.returnValue = '';
  });
}

/**
 * The editing lattice, and nothing else.
 *
 * Which folders this app opened is settled once in `.env` and printed by
 * `run.sh`; repeating it in the header would be a constant with a label. When
 * one of them is missing it is the entry list and the player that say so, at
 * the moment it actually costs you something.
 */
async function loadConfig() {
  try {
    const config = await api.config();
    state.grid = config.annotationGrid || 0.001;
  } catch {
    // The default lattice is the right one; if the server is really gone,
    // loadEntries says so where it can be acted on.
  }
}

/* ------------------------------------------------------------------ browser */

function bindBrowser() {
  $('refreshBtn').addEventListener('click', () => loadEntries({ button: $('refreshBtn') }));

  $('entryFilter').addEventListener('input', (event) => {
    state.entryFilter = event.target.value.trim().toLowerCase();
    renderEntries();
  });

  document.querySelectorAll('.seg-btn[data-entry-sort]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.seg-btn[data-entry-sort]').forEach((other) => {
        other.classList.toggle('is-active', other === button);
      });
      state.entrySort = button.dataset.entrySort;
      renderEntries();
    });
  });

  $('pairToggle').addEventListener('click', () => showPair(true));
  $('browseToggle').addEventListener('click', () => showPair(false));

  $('entriesBody').addEventListener('click', (event) => {
    const row = event.target.closest('tr[data-stem]');
    if (row) openEntry(row.dataset.stem);
  });
}

async function loadEntries({ button = null } = {}) {
  if (button) busy(button, true);
  try {
    const result = await api.entries();
    state.entries = result.entries || [];
    renderEntries();

    const missing = result.missingAudio
      ? ` · ${result.missingAudio} with no audio`
      : '';
    $('browseStatus').textContent = result.datasetOk
      ? `${result.count} entr${result.count === 1 ? 'y' : 'ies'} · ` +
        `${formatTime(result.totalSeconds)} of audio${missing}`
      : 'The dataset folder does not exist. Check ANNOTATEPATH in .env.';
    $('browseStatus').className = `foot-status${result.datasetOk ? '' : ' bad'}`;
  } catch (error) {
    $('browseStatus').textContent = error.message;
    $('browseStatus').className = 'foot-status bad';
  } finally {
    if (button) busy(button, false);
  }
}

/** Entries matching the filter, in the chosen order. */
function visibleEntries() {
  const needle = state.entryFilter;
  const matches = (entry) => {
    if (!needle) return true;
    const hay = [
      entry.stem,
      entry.originalFilename,
      ...(entry.speakers || []),
      entry.annotator?.user,
      entry.annotator?.clientId,
      entry.annotator?.ip,
    ];
    return hay.some((value) => value && String(value).toLowerCase().includes(needle));
  };

  const rows = state.entries.filter(matches);
  const key = state.entrySort;
  rows.sort((a, b) => {
    if (key === 'stem') return a.stem.localeCompare(b.stem);
    if (key === 'durationSeconds') return b.durationSeconds - a.durationSeconds;
    // Newest first; an entry with no sidecar has no date and sorts last.
    return String(b.savedAt || '').localeCompare(String(a.savedAt || ''));
  });
  return rows;
}

function renderEntries() {
  const rows = visibleEntries();
  const body = $('entriesBody');
  body.innerHTML = '';

  for (const entry of rows) {
    body.appendChild(entryRow(entry));
  }

  $('entryCount').textContent = state.entries.length
    ? `${rows.length} of ${state.entries.length}`
    : '';

  const empty = $('entriesEmpty');
  empty.hidden = rows.length > 0;
  if (!rows.length) {
    empty.innerHTML = state.entries.length
      ? 'Nothing matches that filter.'
      : 'No entries yet. Annotate something in the annotation UI, or ' +
        '<strong>upload a pair</strong> below.';
  }
}

function entryRow(entry) {
  const row = document.createElement('tr');
  row.dataset.stem = entry.stem;
  row.classList.toggle('is-open', entry.stem === state.stem);

  const who = entry.annotator || {};
  // user is the only field nginx validated; the rest are what the browser said
  // about itself. Show the strongest one available and label it as a grouping.
  const label = who.user || who.clientId || who.ip;
  const kind = who.user ? 'signed in' : who.clientId ? 'browser id' : who.ip ? 'address only' : '';

  row.innerHTML =
    `<td class="col-entry">
       <span class="entry-name">${escapeHtml(entry.stem)}</span>
       ${entry.originalFilename && entry.originalFilename !== entry.stem
         ? `<span class="entry-file">${escapeHtml(entry.originalFilename)}</span>` : ''}
     </td>
     <td class="col-when">${entry.savedAt
        ? `<span title="${escapeHtml(entry.savedAt)}">${escapeHtml(shortDate(entry.savedAt))}</span>`
        : '<span class="tag warn">no sidecar</span>'}</td>
     <td class="col-num">${formatTime(entry.durationSeconds)}</td>
     <td class="col-num">${entry.segmentCount}${entry.problemCount
        ? ` <span class="tag bad" title="${entry.problemCount} line(s) could not be read">!</span>` : ''}</td>
     <td class="col-speakers">
       <span class="spk-dots" title="${escapeHtml((entry.speakers || []).join(', '))}">
         ${(entry.speakers || []).map((s) =>
           `<i class="spk-dot" style="background:${speakerColor(s)}"></i>`).join('')}
       </span>
       <span class="muted"> ${entry.speakerCount}</span>
     </td>
     <td class="col-annotator">${label
        ? `<span class="who who-id">${escapeHtml(shorten(label))}</span>` +
          `<span class="who who-ip">${escapeHtml(kind)}${who.ip && who.ip !== label
              ? ` · ${escapeHtml(who.ip)}` : ''}</span>`
        : '<span class="muted">unrecorded</span>'}</td>
     <td class="col-audio">${audioTag(entry)}</td>`;
  return row;
}

/**
 * Whether this entry can be listened to.
 *
 * *Where* the audio was found is not shown: it is looked for in the dataset and
 * then in the audios folder automatically, so the distinction changes nothing
 * anyone can act on. Only its absence does.
 */
function audioTag(entry) {
  return entry.hasAudio
    ? ''
    : '<span class="tag bad" title="Neither folder has it">missing</span>';
}

function showPair(on) {
  $('pairCard').hidden = !on;
  $('browseCard').hidden = on;
}

/* --------------------------------------------------------------- open entry */

async function openEntry(stem) {
  if (!confirmDiscard()) return;

  $('browseStatus').textContent = `Opening ${stem}…`;
  $('browseStatus').className = 'foot-status';
  try {
    const detail = await api.entry(stem);
    adopt({
      stem: detail.stem,
      source: 'dataset',
      meta: detail.meta || {},
      audioSource: detail.audioSource,
      segments: detail.segments,
      annotationEnd: detail.annotationEnd,
      duration: detail.durationSeconds,
      problems: detail.problems,
      profiles: (detail.meta || {}).speakerProfiles || [],
    });
    $('browseStatus').textContent = '';
    renderEntries();   // repaint so the open row is highlighted

    if (detail.hasAudio) await loadAudioFrom(api.audioUrl(detail.stem));
    else showNoAudio(detail);
  } catch (error) {
    $('browseStatus').textContent = error.message;
    $('browseStatus').className = 'foot-status bad';
    toast('Could not open that entry', escapeHtml(error.message), 'bad');
  }
}

/**
 * Take an annotation as the thing being edited.
 *
 * Everything downstream reads `state.segments`, so this is the one place that
 * decides what "open" means -- and the one place that snapshots the original,
 * which is what makes Revert honest.
 */
function adopt({ stem, source, meta, audioSource, segments, annotationEnd,
                 duration, problems, profiles }) {
  releaseAudio();

  state.stem = stem;
  state.source = source;
  state.meta = meta || {};
  state.audioSource = audioSource || 'missing';
  state.duration = duration || 0;
  state.annotationEnd = annotationEnd || duration || 0;
  state.selectedId = null;
  state.playingSegmentId = null;

  state.segments = (segments || []).map((item) => makeSegment({
    speaker: item.speaker,
    start: item.start,
    end: item.end,
    text: item.text || '',
  }));
  state.original = fingerprint(state.segments);

  // Speaker colours are deliberately *not* reset per entry: the dots in the
  // list and the rows in the table are the same palette, so speaker_0 is one
  // colour everywhere and a list of entries can be read at a glance.
  speakers.setData(state.segments);
  speakers.setProfiles(profiles || []);

  table.setData(state.segments);
  showProblems(problems || []);

  $('playerCard').hidden = false;
  $('segmentsCard').hidden = false;
  $('speakersCard').hidden = false;
  $('metaCard').hidden = source !== 'dataset';
  if (source === 'dataset') renderMeta();

  $('totalTime').textContent = formatTime(state.duration, true);
  $('currentTime').textContent = formatTime(0, true);
  $('deleteBtn').querySelector('.btn-label').textContent =
    source === 'dataset' ? 'Delete entry' : 'Discard';
  $('saveBtn').querySelector('.btn-label').textContent =
    source === 'dataset' ? 'Save changes' : 'Add to dataset';

  syncWaveform();
  waveform.setTime(0);
  onAnnotationChanged({ reorder: false });
}

/** Fetch once, then use the same bytes for both the waveform and the player. */
async function loadAudioFrom(url) {
  $('waveLoading').hidden = false;
  $('audioMissing').hidden = true;

  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Could not load the audio (HTTP ${response.status}).`);
    const blob = await response.blob();
    await useAudioBlob(blob);
  } catch (error) {
    $('waveLoading').hidden = true;
    warnAudio(`${error.message} The annotation is still editable.`);
  }
}

async function useAudioBlob(blob) {
  state.objectUrl = URL.createObjectURL(blob);
  $('audio').src = state.objectUrl;

  try {
    const decoded = await waveform.load(blob);
    // Trust the decoder over the sidecar: it measured the file in front of it.
    if (decoded > 0) {
      state.duration = decoded;
      state.annotationEnd = Math.max(state.annotationEnd,
                                     Math.ceil(decoded * 1000) / 1000);
      $('totalTime').textContent = formatTime(state.duration, true);
    }
    syncWaveform();
    onAnnotationChanged({ reorder: false });
  } catch (error) {
    warnAudio(
      `The waveform could not be drawn (${error.message}) — playback and ` +
      'editing still work.',
    );
  } finally {
    $('waveLoading').hidden = true;
  }
}

function showNoAudio(detail) {
  const where = detail.originalFilename ? ` (${detail.originalFilename})` : '';
  warnAudio(
    `No audio for this entry${where}: the dataset holds no copy and nothing on ` +
    'the shelf matches its name. Times are being checked against the length ' +
    'recorded when it was saved.',
  );
}

function warnAudio(message) {
  const box = $('audioMissing');
  box.textContent = message;
  box.hidden = false;
}

function releaseAudio() {
  const audio = $('audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  if (state.objectUrl) {
    URL.revokeObjectURL(state.objectUrl);
    state.objectUrl = null;
  }
  $('audioMissing').hidden = true;
}

function showProblems(problems) {
  const box = $('parseProblems');
  box.hidden = problems.length === 0;
  if (!problems.length) return;
  // Listed rather than counted: a line that did not parse is not in the table,
  // so saving would drop it. Whoever is about to press Save should see it.
  box.innerHTML =
    `<strong>${problems.length} line${problems.length === 1 ? '' : 's'} could not be read` +
    ' and are not in the table below.</strong> Saving will drop them.<br>' +
    problems.slice(0, 8).map((problem) =>
      `<code>line ${problem.line}: ${escapeHtml(problem.text.slice(0, 120))}</code> — ` +
      `${escapeHtml(problem.reason)}`).join('<br>') +
    (problems.length > 8 ? `<br>…and ${problems.length - 8} more.` : '');
}

/* ------------------------------------------------------------------ metadata */

function renderMeta() {
  const meta = state.meta || {};
  const who = meta.annotator || {};
  const editor = meta.editedBy || {};
  const der = meta.der || {};

  const fields = [
    ['Entry', state.stem, 'mono'],
    ['Original filename', meta.originalFilename, 'mono'],
    ['Duration', meta.durationSeconds ? formatTime(meta.durationSeconds, true) : null],
    ['Saved', meta.savedAt ? `${shortDate(meta.savedAt)} UTC` : null],
    ['DER', der.derPercent != null ? `${Number(der.derPercent).toFixed(2)}%` : null],
    ['Annotator', who.user, 'mono'],
    ['Browser id', who.clientId, 'mono'],
    ['Address', who.ip, 'mono'],
    ['User agent', who.userAgent],
    ['Last edited here', meta.editedAt ? `${shortDate(meta.editedAt)} UTC` : null],
    ['Edits', meta.editCount ? `${meta.editCount}` : null],
    ['Edited by', editor.user || editor.clientId || editor.ip, 'mono'],
  ];

  $('metaGrid').innerHTML = fields.map(([label, value, cls = '']) => {
    const shown = value === null || value === undefined || value === ''
      ? '<dd class="none">not recorded</dd>'
      : `<dd class="${cls}">${escapeHtml(String(value))}</dd>`;
    return `<div><dt>${escapeHtml(label)}</dt>${shown}</div>`;
  }).join('');

  $('metaRaw').textContent = Object.keys(meta).length
    ? JSON.stringify(meta, null, 2)
    : 'This entry has no sidecar. One will be written the first time it is saved.';
}

/* ------------------------------------------------------------- upload a pair */

function bindPair() {
  bindDrop($('audioDrop'), $('audioInput'), (file) => {
    state.pairFile = file;
    $('audioChosen').textContent = `${file.name} · ${(file.size / (1024 * 1024)).toFixed(1)} MB`;
    $('audioDrop').classList.add('is-set');
    updatePairButton();
  });

  bindDrop($('textDrop'), $('textInput'), (file) => {
    pairText = file;
    $('textChosen').textContent = `${file.name} · ${file.size} bytes`;
    $('textDrop').classList.add('is-set');
    updatePairButton();
  });

  $('openPairBtn').addEventListener('click', openPair);
}

let pairText = null;

function bindDrop(zone, input, onFile) {
  input.addEventListener('change', () => {
    if (input.files?.length) onFile(input.files[0]);
  });
  zone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });
  ['dragenter', 'dragover'].forEach((type) => {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add('is-dragover');
    });
  });
  ['dragleave', 'drop'].forEach((type) => {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.remove('is-dragover');
    });
  });
  zone.addEventListener('drop', (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) onFile(file);
  });
}

function updatePairButton() {
  $('openPairBtn').disabled = !(state.pairFile && pairText);
  $('pairHint').textContent = state.pairFile && pairText
    ? 'Open the pair to check it over before filing it.'
    : 'Both files are needed: the audio to listen to, the annotation to edit.';
}

async function openPair() {
  if (!state.pairFile || !pairText || !confirmDiscard()) return;
  const button = $('openPairBtn');
  busy(button, true);
  $('pairError').hidden = true;

  try {
    const parsed = await api.parse(pairText);
    const file = state.pairFile;
    adopt({
      stem: null,
      source: 'pair',
      meta: {},
      audioSource: 'pair',
      segments: parsed.segments,
      annotationEnd: parsed.annotationEnd,
      duration: parsed.annotationEnd,
      problems: parsed.problems,
      profiles: [],
    });
    await useAudioBlob(file);

    showPair(false);
    // A pair is a candidate, not an entry: it is not saved until asked for.
    markDirty();
    toast(
      'Pair opened',
      `<code>${escapeHtml(file.name)}</code><br>` +
      `${parsed.segments.length} segments — press <strong>Add to dataset</strong> to file it.`,
      'ok',
    );
  } catch (error) {
    $('pairError').textContent = error.message;
    $('pairError').hidden = false;
  } finally {
    busy(button, false);
  }
}

/* ------------------------------------------------------------------- player */

function bindPlayer() {
  const audio = $('audio');

  $('playBtn').addEventListener('click', togglePlay);
  $('backBtn').addEventListener('click', () => seek(audio.currentTime - 5));
  $('fwdBtn').addEventListener('click', () => seek(audio.currentTime + 5));
  $('rateSelect').addEventListener('change', (event) => {
    audio.playbackRate = Number(event.target.value);
  });

  audio.addEventListener('timeupdate', onTimeUpdate);
  audio.addEventListener('play', () => setPlayIcon(true));
  audio.addEventListener('pause', () => setPlayIcon(false));
  audio.addEventListener('ended', () => {
    state.playingSegmentId = null;
    setPlayIcon(false);
  });

  $('zoomIn').addEventListener('click', () => waveform.zoomBy(1.6));
  $('zoomOut').addEventListener('click', () => waveform.zoomBy(1 / 1.6));
  $('zoomFit').addEventListener('click', () => waveform.fit());

  document.addEventListener('keydown', (event) => {
    if (event.target.matches('input, textarea, select')) return;
    if (event.key === ' ') {
      event.preventDefault();
      togglePlay();
    } else if (event.key === 'ArrowLeft') {
      seek(audio.currentTime - (event.shiftKey ? 5 : 1));
    } else if (event.key === 'ArrowRight') {
      seek(audio.currentTime + (event.shiftKey ? 5 : 1));
    }
  });
}

function togglePlay() {
  const audio = $('audio');
  if (!audio.src) return;
  if (audio.paused) {
    state.playingSegmentId = null;
    audio.play().catch(() => {});
  } else {
    audio.pause();
  }
}

function setPlayIcon(playing) {
  // `hidden` as a property does nothing on an SVG element -- it is defined on
  // HTMLElement, so assigning it here just sets a JS expando. The attribute is
  // what the stylesheet acts on.
  $('playBtn').querySelector('.icon-play').toggleAttribute('hidden', playing);
  $('playBtn').querySelector('.icon-pause').toggleAttribute('hidden', !playing);
  if (table) table.setPlaying(table.playingId, playing);
}

function playSegment(id) {
  const audio = $('audio');
  const segment = state.segments.find((item) => item.id === id);
  if (!segment || !audio.src) return;

  if (!audio.paused && state.playingSegmentId === id) {
    audio.pause();
    return;
  }

  state.playingSegmentId = id;
  seek(segment.start ?? 0);
  audio.play().catch(() => { state.playingSegmentId = null; });
}

function seek(seconds) {
  const audio = $('audio');
  const time = Math.min(Math.max(seconds, 0), state.duration || 0);
  if (audio.src) audio.currentTime = time;
  waveform.setTime(time);
  $('currentTime').textContent = formatTime(time, true);
}

function onTimeUpdate() {
  const audio = $('audio');
  const time = audio.currentTime;
  $('currentTime').textContent = formatTime(time, true);
  waveform.setTime(time);

  if (state.playingSegmentId && !$('loopSegment').checked) {
    const played = state.segments.find((item) => item.id === state.playingSegmentId);
    if (played && played.end !== null && time >= played.end) {
      audio.pause();
      seek(played.end);
      state.playingSegmentId = null;
      return;
    }
  }

  if ($('loopSegment').checked && state.selectedId) {
    const segment = state.segments.find((item) => item.id === state.selectedId);
    if (segment && segment.end !== null && time >= segment.end) {
      audio.currentTime = segment.start ?? 0;
      return;
    }
  }

  // Overlapping speech puts the playhead in several rows at once; the one
  // whose own button started this is the one being listened to.
  const playing = state.playingSegmentId
    ? state.segments.find((segment) => segment.id === state.playingSegmentId)
    : state.segments.find(
        (segment) => segment.start !== null && segment.end !== null &&
          time >= segment.start && time < segment.end,
      );
  table.setPlaying(playing ? playing.id : null, !audio.paused);
}

/* ---------------------------------------------------------------- selection */

function select(id) {
  state.selectedId = id;
  table.setSelected(id);
  waveform.setSelected(id);
}

function editStep() {
  return state.milliseconds ? state.grid : 1;
}

function dragEdge(id, side, time, commit) {
  const segment = state.segments.find((item) => item.id === id);
  if (!segment) return;

  const step = editStep();
  const snapped = snapTime(time, step, 0, state.annotationEnd);
  if (side === 'start') {
    segment.start = Math.min(snapped, (segment.end ?? state.annotationEnd) - step);
  } else {
    segment.end = Math.max(snapped, (segment.start ?? 0) + step);
  }

  table.syncTimes(id);
  syncWaveform();
  onAnnotationChanged({ reorder: commit });
}

function syncWaveform() {
  waveform.setSegments(sortSegments(state.segments, 'time'), state.selectedId);
}

/* ------------------------------------------------------------------ toolbar */

function bindToolbar() {
  document.querySelectorAll('.seg-btn[data-sort]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.seg-btn[data-sort]').forEach((other) => {
        other.classList.toggle('is-active', other === button);
      });
      table.setSortMode(button.dataset.sort);
    });
  });

  $('msToggle').addEventListener('change', (event) => {
    state.milliseconds = event.target.checked;
    table.setMilliseconds(state.milliseconds);
  });

  $('addSegment').addEventListener('click', addSegment);
  $('saveBtn').addEventListener('click', save);
  $('revertBtn').addEventListener('click', revert);
  $('deleteBtn').addEventListener('click', removeEntry);
}

/**
 * Where the next segment for `speaker` can go without overlapping itself.
 *
 * Searches forward from `from` and then wraps, so repeated adds march along
 * the gaps instead of stacking on one spot. Null when that speaker already
 * covers the whole file -- a row landing inside its own speaker would be
 * flagged invalid the moment it appeared.
 */
function freeSlot(speaker, from, step, length) {
  const busySpans = state.segments
    .filter((item) => item.speaker === speaker && item.start !== null && item.end !== null)
    .map((item) => [item.start, item.end])
    .sort((a, b) => a[0] - b[0]);

  const gaps = [];
  let cursor = 0;
  for (const [start, end] of busySpans) {
    if (start - cursor > 1e-6) gaps.push([cursor, start]);
    cursor = Math.max(cursor, end);
  }
  if (state.annotationEnd - cursor > 1e-6) gaps.push([cursor, state.annotationEnd]);

  const snapUp = (time) => Math.ceil(time / step - 1e-6) * step;
  const snapDown = (time) => Math.floor(time / step + 1e-6) * step;

  const order = [...gaps.filter((g) => g[1] > from), ...gaps.filter((g) => g[1] <= from)];
  for (const [low, high] of order) {
    const start = snapUp(Math.max(low, Math.min(from, high)));
    const end = Math.min(snapDown(high), start + length);
    if (end - start >= step - 1e-6) {
      return { start: Number(start.toFixed(6)), end: Number(end.toFixed(6)) };
    }
  }
  return null;
}

function addSegment() {
  if (!state.annotationEnd) return;
  const audio = $('audio');
  const NEW_LENGTH = 1;
  const step = editStep();
  const speaker = uniqueSpeakers(state.segments)[0] || 'speaker_0';
  const from = snapTime(audio.currentTime || 0, step, 0, state.annotationEnd);

  const slot = freeSlot(speaker, from, step, NEW_LENGTH);
  if (!slot) {
    toast(
      'No room for another segment',
      `<span class="toast-note">${escapeHtml(speaker)} already speaks for the ` +
        'whole file. Retime or delete one of its segments first — a speaker ' +
        'cannot overlap itself.</span>',
      'bad',
    );
    return;
  }

  const segment = makeSegment({ speaker, start: slot.start, end: slot.end, text: '' });
  state.segments.push(segment);

  table.setData(state.segments);
  select(segment.id);
  table.focusSegment(segment.id);
  syncWaveform();
  onAnnotationChanged({ reorder: false });
}

/* ------------------------------------------------------------------- change */

/** A stable string for the annotation, so "has this changed?" is one compare. */
function fingerprint(segments) {
  return JSON.stringify(
    sortSegments(segments, 'time').map((s) => [s.speaker, s.start, s.end, s.text]),
  );
}

function isDirty() {
  return Boolean(state.source) && (
    state.source === 'pair' || fingerprint(state.segments) !== state.original
  );
}

function markDirty() {
  onAnnotationChanged({ reorder: false });
}

/** Ask before throwing edits away. Returns false when the answer is "don't". */
function confirmDiscard() {
  if (!isDirty()) return true;
  const what = state.source === 'pair'
    ? 'This pair has not been filed yet.'
    : `${state.stem} has unsaved edits.`;
  return window.confirm(`${what}\n\nOpen something else and lose them?`);
}

function onAnnotationChanged({ reorder = false } = {}) {
  state.errors = validateSegments(
    state.segments, 0, state.annotationEnd, state.milliseconds);
  speakers.setData(state.segments);
  table.setErrors(state.errors);
  syncWaveform();

  if (reorder) {
    // Re-sorting mid-keystroke would move the row out from under the caret, so
    // this only runs once an edit is committed.
    table.render();
    table.setErrors(state.errors);
  }

  const invalid = state.errors.size > 0;
  const dirty = isDirty();

  $('saveBtn').disabled =
    state.loading || invalid || !state.segments.length || !dirty;
  $('revertBtn').disabled = state.loading || !dirty || state.source !== 'dataset';
  $('deleteBtn').disabled = state.loading || !state.source;

  const status = $('saveStatus');
  if (invalid) {
    status.textContent = 'Fix the highlighted segments before saving.';
    status.className = 'foot-status bad';
  } else if (state.source === 'pair') {
    status.textContent = 'Not in the dataset yet.';
    status.className = 'foot-status dirty';
  } else if (dirty) {
    status.textContent = 'Unsaved edits.';
    status.className = 'foot-status dirty';
  } else if (state.stem) {
    status.textContent = `${state.stem} — no unsaved edits.`;
    status.className = 'foot-status';
  } else {
    status.textContent = '';
    status.className = 'foot-status';
  }
}

/* --------------------------------------------------------------------- save */

function annotationPayload() {
  return {
    segments: sortSegments(state.segments, 'time').map((segment) => ({
      speaker: segment.speaker,
      start: segment.start,
      end: segment.end,
      transcript: segment.text,
    })),
    speakers: speakers.getProfiles(),
    audioStart: 0,
    audioEnd: state.annotationEnd,
    clientId: clientId(),
  };
}

async function save() {
  if (!state.segments.length || !state.source) return;
  const button = $('saveBtn');
  state.loading = true;
  busy(button, true);

  try {
    const result = state.source === 'pair'
      ? await api.createEntry({
          file: state.pairFile,
          payload: annotationPayload(),
          clientId: clientId(),
        })
      : await api.updateEntry(state.stem, annotationPayload());

    state.original = fingerprint(state.segments);
    if (result.created) {
      // It is a dataset entry now, not a candidate: the footer, the metadata
      // panel and Revert all behave differently from here on.
      state.stem = result.stem;
      state.source = 'dataset';
      state.pairFile = null;
      $('deleteBtn').querySelector('.btn-label').textContent = 'Delete entry';
      $('saveBtn').querySelector('.btn-label').textContent = 'Save changes';
    }

    toast(
      result.created ? 'Added to dataset' : 'Saved',
      `<code>${escapeHtml(result.stem)}.txt</code>` +
        `<span class="toast-note">${result.segmentCount} segments · ` +
        `edit ${result.editCount} of this entry</span>`,
      'ok',
    );

    await loadEntries();
    // Re-read so the metadata panel shows what actually landed on disk rather
    // than what this page believes it sent.
    const detail = await api.entry(state.stem);
    state.meta = detail.meta || {};
    state.audioSource = detail.audioSource;
    renderMeta();
    $('metaCard').hidden = false;
  } catch (error) {
    $('saveStatus').textContent = error.message;
    $('saveStatus').className = 'foot-status bad';
    toast('Save failed', escapeHtml(error.message), 'bad');
  } finally {
    state.loading = false;
    busy(button, false);
    onAnnotationChanged({ reorder: true });
  }
}

async function revert() {
  if (state.source !== 'dataset' || !state.stem) return;
  if (isDirty() && !window.confirm(
    `Throw away every edit to ${state.stem} and re-read it from disk?`)) return;

  const button = $('revertBtn');
  busy(button, true);
  const stem = state.stem;
  // Nothing to discard any more: skip the "are you sure" that openEntry asks.
  state.original = fingerprint(state.segments);
  state.source = null;
  try {
    await openEntry(stem);
  } finally {
    busy(button, false);
  }
}

/* ------------------------------------------------------------------- remove */

// Removal is not undoable, so the first click only arms the button; the second
// within a few seconds does the work.
let removeTimer = null;

function disarmRemove() {
  clearTimeout(removeTimer);
  removeTimer = null;
  const button = $('deleteBtn');
  button.classList.remove('is-armed');
  button.querySelector('.btn-label').textContent =
    state.source === 'pair' ? 'Discard' : 'Delete entry';
}

async function removeEntry() {
  if (!state.source) return;

  // An unfiled pair is only in this page; closing it costs nobody anything.
  if (state.source === 'pair') {
    closeOpen();
    return;
  }

  if (removeTimer === null) {
    $('deleteBtn').classList.add('is-armed');
    $('deleteBtn').querySelector('.btn-label').textContent = 'Click again to delete';
    removeTimer = setTimeout(disarmRemove, 6000);
    return;
  }
  disarmRemove();

  const button = $('deleteBtn');
  const stem = state.stem;
  const wasOnShelf = state.audioSource === 'audios';
  state.loading = true;
  busy(button, true);
  try {
    const result = await api.removeEntry(stem);
    closeOpen();
    await loadEntries();
    toast(
      'Deleted from dataset',
      result.removed.map((name) => `<code>${escapeHtml(name)}</code>`).join('<br>') +
        (wasOnShelf
          ? '<span class="toast-note">Its audio is not in the dataset and was ' +
            'left where it is.</span>'
          : ''),
      'ok',
    );
  } catch (error) {
    toast('Delete failed', escapeHtml(error.message), 'bad');
  } finally {
    state.loading = false;
    busy(button, false);
  }
}

/** Close whatever is open, without touching anything on disk. */
function closeOpen() {
  releaseAudio();
  state.audioSourceWas = state.audioSource;
  state.stem = null;
  state.playingSegmentId = null;
  state.source = null;
  state.segments = [];
  state.original = '';
  state.pairFile = null;
  pairText = null;
  state.meta = {};
  state.audioSource = 'missing';

  table.setData([]);
  speakers.clear();
  $('playerCard').hidden = true;
  $('segmentsCard').hidden = true;
  $('speakersCard').hidden = true;
  $('metaCard').hidden = true;
  $('parseProblems').hidden = true;
  $('audioDrop').classList.remove('is-set');
  $('textDrop').classList.remove('is-set');
  $('audioChosen').textContent = 'WAV, MP3, FLAC, OGG, M4A, WebM';
  $('textChosen').innerHTML =
    'One <code>speaker start end transcript</code> per line — not JSON';
  updatePairButton();
  renderEntries();
}

/* ------------------------------------------------------------------ helpers */

function busy(button, isBusy) {
  button.disabled = isBusy;
  button.classList.toggle('is-busy', isBusy);
  const spinner = button.querySelector('.spinner');
  if (spinner) spinner.hidden = !isBusy;
}

/** "2026-08-21T03:26:19+00:00" -> "2026-08-21 03:26". */
function shortDate(value) {
  const text = String(value);
  return text.length >= 16 ? `${text.slice(0, 10)} ${text.slice(11, 16)}` : text;
}

function shorten(value, keep = 12) {
  const text = String(value);
  return text.length > keep ? `${text.slice(0, keep)}…` : text;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

function toast(title, bodyHtml, kind = 'ok') {
  const element = document.createElement('div');
  element.className = `toast ${kind}`;
  element.innerHTML = `<strong>${escapeHtml(title)}</strong>${bodyHtml}`;
  $('toasts').appendChild(element);
  setTimeout(() => element.remove(), 8000);
}

init();
