/**
 * Canvas waveform with a scrubbable playhead, speaker regions and zoom.
 *
 * Rendering approach: the audio is reduced once to a min/max peak array, and
 * every frame draws only the visible slice of that array. The canvas itself is
 * `position: sticky` inside a horizontally scrollable spacer, so the browser
 * supplies a real scrollbar while we redraw from `scrollLeft` -- no custom
 * scrollbar, and panning stays smooth on long files.
 *
 * The drawing itself follows Gradio, which is wavesurfer.js configured with
 * `barWidth: 2, barGap: 3, barRadius: 10, normalize: true, minPxPerSec: 20`
 * and its `waveColor`/`progressColor`/`cursorColor` defaults. The two things
 * that matter for long audio are in there: a *fixed* timescale, so an hour is
 * an hour's worth of strip and scrolls rather than being stretched onto one
 * screen, and rounded bars at a constant pitch, which look the same at every
 * length instead of turning into a smeared ribbon.
 */

import { speakerColor } from './annotation.js';

const PEAKS_PER_SECOND = 400;      // ~2.5 ms resolution
const MAX_PEAKS = 400_000;         // ~3 MB of Float32, caps very long files
const LABEL_LANE = 16;             // px reserved at the top for speaker labels
const EDGE_GRAB_PX = 6;            // hit slop for dragging a segment boundary
const MAX_PX_PER_SECOND = 400;

// Gradio's waveform, verbatim.
const BAR_WIDTH = 2;
const BAR_GAP = 3;
const BAR_RADIUS = 10;             // clamped to half the bar: a rounded pill
const WAVE_COLOR = '#9ca3af';
const PROGRESS_COLOR = '#f97316';
const CURSOR_COLOR = '#ddd5e9';
const CURSOR_WIDTH = 2;
const MIN_PX_PER_SECOND = 20;      // the scale a file opens at

export class Waveform {
  constructor({ scrollEl, spacerEl, canvasEl, rulerEl, onSeek, onSelect, onEdgeDrag }) {
    this.scrollEl = scrollEl;
    this.spacerEl = spacerEl;
    this.canvas = canvasEl;
    this.ruler = rulerEl;
    this.ctx = canvasEl.getContext('2d');

    this.onSeek = onSeek || (() => {});
    this.onSelect = onSelect || (() => {});
    this.onEdgeDrag = onEdgeDrag || (() => {});

    this.duration = 0;
    this.peaks = null;
    this.segments = [];
    this.selectedId = null;
    this.time = 0;
    this.pxPerSecond = 0;
    this.minPxPerSecond = 0;
    this.follow = true;

    this.drag = null;
    this.hoverEdge = null;
    this.frame = null;
    // Segment edges stay put until this browser is allowed to edit.
    this.locked = true;
    // Bumped on every load and unload, so a decode that finishes after the
    // strip was cleared cannot paint itself back on.
    this.generation = 0;

    this._bindEvents();
  }

  /* ---------------------------------------------------------------- audio */

  /**
   * Decode `file` and build the peak array. Returns the decoded duration,
   * or null when a newer load or an unload won the race.
   */
  async load(file) {
    const generation = ++this.generation;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) throw new Error('This browser has no Web Audio support.');

    const buffer = await file.arrayBuffer();
    if (generation !== this.generation) return null;
    const context = new AudioCtx();
    let decoded;
    try {
      decoded = await context.decodeAudioData(buffer);
    } catch (error) {
      if (generation !== this.generation) return null;
      throw error;
    } finally {
      context.close();
    }
    if (generation !== this.generation) return null;

    this.duration = decoded.duration;
    this.peaks = buildPeaks(decoded);
    this.time = 0;
    this.openingView();
    return this.duration;
  }

  /** Refuse edge drags. Seeking and playback are unchanged. */
  setLocked(locked) {
    this.locked = Boolean(locked);
    if (this.locked) {
      this.hoverEdge = null;
      this.drag = this.drag && this.drag.kind === 'edge' ? null : this.drag;
      this.canvas.classList.remove('is-resizing');
    }
  }

  /** Drop the decoded audio so the strip draws empty. */
  unload() {
    this.generation += 1;
    this.duration = 0;
    this.peaks = null;
    this.time = 0;
    this.segments = [];
    this.selectedId = null;
    this.pxPerSecond = 0;
    this.drag = null;
    this.hoverEdge = null;
    this.canvas.classList.remove('is-resizing', 'is-grabbing');
    this.spacerEl.style.width = '';
    this.scrollEl.scrollLeft = 0;
    if (this.ruler) this.ruler.innerHTML = '';
    this.render();
  }

  /* ------------------------------------------------------------- geometry */

  get viewportWidth() {
    return this.scrollEl.clientWidth || 1;
  }

  fit() {
    this.minPxPerSecond = this.duration > 0 ? this.viewportWidth / this.duration : 1;
    this.setZoom(this.minPxPerSecond);
    this.scrollEl.scrollLeft = 0;
  }

  /**
   * The view a file opens in: Gradio's `minPxPerSec`, and nothing cleverer.
   *
   * Twenty pixels a second whatever the file is, so audio is drawn at one
   * constant scale and length becomes distance -- an hour is an hour of strip
   * to scroll along, not an hour squeezed into a screen at half a pixel a
   * second, where a two-second segment is a hairline and no boundary can be
   * grabbed. A file too short to fill the strip at that scale is stretched to
   * fit it instead, exactly as wavesurfer does, since zooming out past the fit
   * is not allowed anyway.
   */
  openingView() {
    this.minPxPerSecond = this.duration > 0 ? this.viewportWidth / this.duration : 1;
    this.setZoom(Math.max(this.minPxPerSecond, MIN_PX_PER_SECOND));
    this.scrollEl.scrollLeft = 0;
  }

  setZoom(pxPerSecond, anchorTime = null) {
    if (!this.duration) return;
    this.minPxPerSecond = this.viewportWidth / this.duration;
    const next = clamp(pxPerSecond, this.minPxPerSecond, MAX_PX_PER_SECOND);
    // Keep `anchorTime` under the same screen x across the zoom change.
    const anchor = anchorTime ?? this.centreTime();
    const offsetPx = anchorTime !== null
      ? anchorTime * this.pxPerSecond - this.scrollEl.scrollLeft
      : this.viewportWidth / 2;

    this.pxPerSecond = next;
    this.spacerEl.style.width = `${Math.max(this.viewportWidth, this.duration * next)}px`;
    this.scrollEl.scrollLeft = anchor * next - offsetPx;
    this.render();
  }

  zoomBy(factor) {
    this.setZoom(this.pxPerSecond * factor, this.time);
  }

  centreTime() {
    return (this.scrollEl.scrollLeft + this.viewportWidth / 2) / (this.pxPerSecond || 1);
  }

  timeAt(clientX) {
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left + this.scrollEl.scrollLeft;
    return clamp(x / (this.pxPerSecond || 1), 0, this.duration);
  }

  xOf(seconds) {
    return seconds * this.pxPerSecond - this.scrollEl.scrollLeft;
  }

  /* ---------------------------------------------------------------- state */

  setSegments(segments, selectedId = this.selectedId) {
    this.segments = segments;
    this.selectedId = selectedId;
    this.render();
  }

  setSelected(id) {
    this.selectedId = id;
    this.render();
  }

  setTime(seconds, { follow = true } = {}) {
    this.time = clamp(seconds, 0, this.duration || 0);
    if (follow && this.follow) this.keepVisible(this.time);
    this.render();
  }

  keepVisible(seconds) {
    const x = this.xOf(seconds);
    const width = this.viewportWidth;
    if (x < 0 || x > width) {
      // Re-centre rather than nudge: during playback a nudge re-triggers
      // constantly at the edge.
      this.scrollEl.scrollLeft = seconds * this.pxPerSecond - width / 2;
    }
  }

  /* --------------------------------------------------------------- events */

  _bindEvents() {
    this.scrollEl.addEventListener('scroll', () => this.render(), { passive: true });

    this.scrollEl.addEventListener('wheel', (event) => {
      if (!this.duration) return;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        this.setZoom(
          this.pxPerSecond * (event.deltaY < 0 ? 1.15 : 1 / 1.15),
          this.timeAt(event.clientX),
        );
      } else if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
        // Vertical wheel over a horizontal-only strip should pan it.
        const before = this.scrollEl.scrollLeft;
        this.scrollEl.scrollLeft += event.deltaY;
        if (this.scrollEl.scrollLeft !== before) event.preventDefault();
      }
    }, { passive: false });

    this.canvas.addEventListener('pointerdown', (event) => {
      if (!this.duration || event.button !== 0) return;
      const time = this.timeAt(event.clientX);
      const edge = this.locked ? null : this.edgeAt(event.clientX);

      this.canvas.setPointerCapture(event.pointerId);

      if (edge) {
        this.drag = { kind: 'edge', ...edge };
        this.canvas.classList.add('is-resizing');
      } else {
        this.drag = { kind: 'scrub' };
        const hit = this.segmentAt(time);
        this.onSelect(hit ? hit.id : null);
        this.onSeek(time);
      }
      this.render();
    });

    this.canvas.addEventListener('pointermove', (event) => {
      if (!this.duration) return;

      if (!this.drag) {
        if (this.locked) return;
        const edge = this.edgeAt(event.clientX);
        const changed = (edge && edge.side) !== (this.hoverEdge && this.hoverEdge.side);
        this.hoverEdge = edge;
        this.canvas.classList.toggle('is-resizing', Boolean(edge));
        if (changed) this.render();
        return;
      }

      const time = this.timeAt(event.clientX);
      if (this.drag.kind === 'scrub') {
        this.onSeek(time);
      } else {
        this.onEdgeDrag(this.drag.id, this.drag.side, time, false);
      }
    });

    const endDrag = (event) => {
      if (!this.drag) return;
      if (this.drag.kind === 'edge') {
        this.onEdgeDrag(this.drag.id, this.drag.side, this.timeAt(event.clientX), true);
      }
      this.drag = null;
      this.canvas.classList.remove('is-resizing', 'is-grabbing');
      this.render();
    };
    this.canvas.addEventListener('pointerup', endDrag);
    this.canvas.addEventListener('pointercancel', endDrag);

    this.resizeObserver = new ResizeObserver(() => {
      if (!this.duration) return;
      const wasFit = this.pxPerSecond <= this.minPxPerSecond + 1e-9;
      this.minPxPerSecond = this.viewportWidth / this.duration;
      this.setZoom(wasFit ? this.minPxPerSecond : this.pxPerSecond);
    });
    this.resizeObserver.observe(this.scrollEl);
  }

  /** The segment boundary near `clientX`, if the pointer is close enough. */
  edgeAt(clientX) {
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    let best = null;
    for (const segment of this.segments) {
      if (segment.start === null || segment.end === null) continue;
      for (const side of ['start', 'end']) {
        const distance = Math.abs(this.xOf(segment[side]) - x);
        if (distance <= EDGE_GRAB_PX && (!best || distance < best.distance)) {
          best = { id: segment.id, side, distance };
        }
      }
    }
    return best;
  }

  segmentAt(time) {
    // Later segments win, matching what is drawn on top.
    for (let i = this.segments.length - 1; i >= 0; i -= 1) {
      const segment = this.segments[i];
      if (segment.start !== null && segment.end !== null &&
          time >= segment.start && time <= segment.end) {
        return segment;
      }
    }
    return null;
  }

  /* -------------------------------------------------------------- drawing */

  render() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this._draw();
    });
  }

  _draw() {
    const { ctx, canvas } = this;
    const dpr = window.devicePixelRatio || 1;
    const width = this.viewportWidth;
    const height = canvas.clientHeight || 168;

    // The canvas is sticky *inside* the spacer, so `width: 100%` would be 100%
    // of the whole scrollable length -- one viewport's worth of drawing stretched
    // across an hour of strip. It is pinned to the viewport instead, and only
    // the backing store follows the device pixel ratio.
    if (canvas.style.width !== `${width}px`) canvas.style.width = `${width}px`;
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    ctx.fillStyle = '#0a0e14';
    ctx.fillRect(0, 0, width, height);

    if (!this.peaks || !this.duration) return;

    const waveTop = LABEL_LANE;
    const waveHeight = height - LABEL_LANE;
    const mid = waveTop + waveHeight / 2;

    this._drawSegments(ctx, width, height, waveTop);
    this._drawPeaks(ctx, width, mid, waveHeight);
    this._drawPlayhead(ctx, height);
    this._drawRuler(width);
  }

  /**
   * Rounded bars at a constant pitch, in wave and progress colours.
   *
   * A bar covers its whole slot -- the gap included -- so no audio falls
   * between two of them, and the slots are laid out in *document* pixels
   * rather than from the left edge of the viewport, so bars stay nailed to the
   * audio while scrolling instead of shimmering under it. Quiet passages keep
   * a bar the width of a dot rather than vanishing, which is what makes silence
   * read as silence and not as a gap in the drawing.
   */
  _drawPeaks(ctx, width, mid, waveHeight) {
    const { min, max, bucketsPerSecond, peak } = this.peaks;
    const slot = BAR_WIDTH + BAR_GAP;
    const amplitude = (waveHeight / 2) * 0.92;
    const gain = peak > 0 ? 1 / peak : 1;   // Gradio draws normalized
    const radius = Math.min(BAR_RADIUS, BAR_WIDTH / 2);
    const scrollLeft = this.scrollEl.scrollLeft;
    const playhead = this.xOf(this.time);
    const rounded = typeof ctx.roundRect === 'function';

    const first = Math.floor(scrollLeft / slot);
    const last = Math.ceil((scrollLeft + width) / slot);

    // Two passes rather than a fillStyle change per bar: the played bars and
    // the rest are each one path.
    for (const played of [false, true]) {
      ctx.fillStyle = played ? PROGRESS_COLOR : WAVE_COLOR;
      ctx.beginPath();
      for (let index = first; index <= last; index += 1) {
        const x = index * slot - scrollLeft;
        if ((x + BAR_WIDTH <= playhead) !== played) continue;

        const t0 = (index * slot) / this.pxPerSecond;
        const t1 = ((index + 1) * slot) / this.pxPerSecond;
        if (t0 >= this.duration) continue;

        let i0 = Math.floor(t0 * bucketsPerSecond);
        let i1 = Math.ceil(t1 * bucketsPerSecond);
        if (i1 <= i0) i1 = i0 + 1;
        i0 = Math.max(0, Math.min(i0, min.length - 1));
        i1 = Math.max(i0 + 1, Math.min(i1, min.length));

        let lo = 0;
        let hi = 0;
        for (let i = i0; i < i1; i += 1) {
          if (min[i] < lo) lo = min[i];
          if (max[i] > hi) hi = max[i];
        }

        let top = mid - Math.min(1, hi * gain) * amplitude;
        let bottom = mid + Math.min(1, -lo * gain) * amplitude;
        if (bottom - top < BAR_WIDTH) {
          const centre = (top + bottom) / 2;
          top = centre - BAR_WIDTH / 2;
          bottom = centre + BAR_WIDTH / 2;
        }
        if (rounded) ctx.roundRect(x, top, BAR_WIDTH, bottom - top, radius);
        else ctx.rect(x, top, BAR_WIDTH, bottom - top);
      }
      ctx.fill();
    }
  }

  _drawSegments(ctx, width, height, waveTop) {
    const visibleStart = this.scrollEl.scrollLeft / this.pxPerSecond;
    const visibleEnd = visibleStart + width / this.pxPerSecond;

    for (const segment of this.segments) {
      if (segment.start === null || segment.end === null) continue;
      if (segment.end < visibleStart || segment.start > visibleEnd) continue;

      const x0 = this.xOf(segment.start);
      const x1 = this.xOf(segment.end);
      const bandWidth = Math.max(1, x1 - x0);
      const color = speakerColor(segment.speaker);
      const selected = segment.id === this.selectedId;

      ctx.fillStyle = hexToRgba(color, selected ? 0.24 : 0.11);
      ctx.fillRect(x0, waveTop, bandWidth, height - waveTop);

      ctx.fillStyle = hexToRgba(color, selected ? 0.95 : 0.6);
      ctx.fillRect(x0, 0, bandWidth, LABEL_LANE - 3);

      ctx.strokeStyle = hexToRgba(color, selected ? 0.95 : 0.45);
      ctx.lineWidth = selected ? 2 : 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(x0) + 0.5, waveTop);
      ctx.lineTo(Math.round(x0) + 0.5, height);
      ctx.moveTo(Math.round(x1) - 0.5, waveTop);
      ctx.lineTo(Math.round(x1) - 0.5, height);
      ctx.stroke();

      if (bandWidth > 34) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(x0, 0, bandWidth, LABEL_LANE);
        ctx.clip();
        ctx.fillStyle = '#06121f';
        ctx.font = '600 9px ui-monospace, SFMono-Regular, Menlo, monospace';
        ctx.textBaseline = 'middle';
        ctx.fillText(segment.speaker, x0 + 4, (LABEL_LANE - 3) / 2 + 0.5);
        ctx.restore();
      }

      if (selected) {
        ctx.fillStyle = hexToRgba(color, 0.95);
        for (const x of [x0, x1]) {
          ctx.fillRect(Math.round(x) - 2, waveTop, 4, height - waveTop);
        }
      }
    }
  }

  _drawPlayhead(ctx, height) {
    const x = Math.round(this.xOf(this.time));
    ctx.fillStyle = CURSOR_COLOR;
    ctx.fillRect(x - CURSOR_WIDTH / 2, 0, CURSOR_WIDTH, height);

    // wavesurfer stops at the line; this strip is also scrubbed and retimed by
    // hand, so the head keeps a tab you can aim at.
    ctx.beginPath();
    ctx.moveTo(x - 5, 0);
    ctx.lineTo(x + 5, 0);
    ctx.lineTo(x, 7);
    ctx.closePath();
    ctx.fill();
  }

  _drawRuler(width) {
    if (!this.ruler) return;
    const step = niceStep(this.pxPerSecond);
    const startTime = this.scrollEl.scrollLeft / this.pxPerSecond;
    const endTime = startTime + width / this.pxPerSecond;

    const marks = [];
    for (let t = Math.ceil(startTime / step) * step; t <= endTime; t += step) {
      marks.push(`<span style="left:${this.xOf(t).toFixed(1)}px">${rulerLabel(t, step)}</span>`);
    }
    this.ruler.innerHTML = marks.join('');
  }

  destroy() {
    this.resizeObserver?.disconnect();
    if (this.frame) cancelAnimationFrame(this.frame);
  }
}

/* ------------------------------------------------------------------ helpers */

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

function buildPeaks(audioBuffer) {
  const length = audioBuffer.length;
  const bucketCount = Math.max(
    1,
    Math.min(MAX_PEAKS, Math.ceil(audioBuffer.duration * PEAKS_PER_SECOND)),
  );
  const bucketsPerSecond = bucketCount / audioBuffer.duration;

  // Averaging every channel doubles the work on stereo for no visible gain;
  // two channels is enough to represent the mix.
  const channels = [];
  for (let c = 0; c < Math.min(2, audioBuffer.numberOfChannels); c += 1) {
    channels.push(audioBuffer.getChannelData(c));
  }

  const min = new Float32Array(bucketCount);
  const max = new Float32Array(bucketCount);
  const samplesPerBucket = length / bucketCount;
  let peak = 0;   // the loudest sample, for Gradio's `normalize: true`

  for (let bucket = 0; bucket < bucketCount; bucket += 1) {
    const from = Math.floor(bucket * samplesPerBucket);
    const to = Math.min(length, Math.max(from + 1, Math.floor((bucket + 1) * samplesPerBucket)));
    let lo = 0;
    let hi = 0;
    for (let i = from; i < to; i += 1) {
      let value = channels[0][i];
      if (channels.length > 1) value = (value + channels[1][i]) / 2;
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    }
    min[bucket] = lo;
    max[bucket] = hi;
    if (hi > peak) peak = hi;
    if (-lo > peak) peak = -lo;
  }
  return { min, max, bucketsPerSecond, peak };
}

function hexToRgba(hex, alpha) {
  const value = hex.replace('#', '');
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Tick spacing that keeps labels ~70 px apart at the current zoom. */
function niceStep(pxPerSecond) {
  const target = 70 / (pxPerSecond || 1);
  const steps = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  return steps.find((step) => step >= target) ?? 3600;
}

function rulerLabel(seconds, step) {
  const value = Math.max(0, seconds);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  const secText = step < 1 ? secs.toFixed(1).padStart(4, '0') : String(Math.round(secs)).padStart(2, '0');
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${secText}`
    : `${minutes}:${secText}`;
}
