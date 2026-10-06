/* Zuschnitt an der Wellenform (U8, R41). Progressive Enhancement: ohne JS
 * bleiben die Zeitfelder Start/Ende das vollstaendige Werkzeug.
 *
 * Griffe und Zeitfelder halten sich gegenseitig synchron. Die Wellenform
 * wird per fetch + decodeAudioData gezeichnet; scheitert das (z. B. CORS
 * am Objektspeicher), arbeiten die Griffe trotzdem gegen die Dauer des
 * <audio>-Elements, nur ohne Zeichnung.
 *
 * Vorschau (docs/design/zuschnitt-vorschau-mockup.html): "Ausschnitt",
 * "Anfang" und "Schluss" anhoeren, dazu beim Ziehen eines Griffs kurze
 * Hoerproben genau an der Schnittkante. Mit dekodierter Datei laeuft das
 * ueber WebAudio; ohne sie ueber das <audio>-Element, dann ohne Mithoeren.
 */
(function () {
  "use strict";

  const form = document.querySelector("[data-role='trim-form']");
  const wave = form && form.querySelector(".wave");
  const audio = document.querySelector("audio[data-role='audio']");
  if (!form || !wave || !audio) return;

  const startInput = form.querySelector("[data-role='cut-start']");
  const endInput = form.querySelector("[data-role='cut-end']");
  const startHandle = wave.querySelector("[data-role='handle-start']");
  const endHandle = wave.querySelector("[data-role='handle-end']");
  const cutLeft = wave.querySelector("[data-role='cut-left']");
  const cutRight = wave.querySelector("[data-role='cut-right']");
  const summary = form.querySelector("[data-role='cut-summary']");
  const canvas = wave.querySelector("canvas");
  const playhead = wave.querySelector("[data-role='playhead']");
  const preview = form.querySelector("[data-role='preview']");
  const live = form.querySelector("[data-role='live']");

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  let audioCtx = null;
  let buffer = null;
  let source = null;
  let frame = 0;
  let lastGrain = 0;
  const GRAIN = 0.35; // Sekunden je Hoerprobe beim Ziehen
  const GRAIN_EVERY_MS = 90;

  let duration = 0;
  let start = 0;
  let end = 0;

  // Gleiche Formate wie app/admin/review.py::parse_time/format_time.
  function parseTime(raw) {
    const text = raw.trim().replace(",", ".");
    if (!text) return null;
    let value;
    if (text.includes(":")) {
      const [m, s] = text.split(":");
      value = Number(m) * 60 + Number(s);
    } else {
      value = Number(text);
    }
    return Number.isFinite(value) && value >= 0 ? value : NaN;
  }

  function formatTime(seconds) {
    const t = Math.round(seconds * 10);
    const m = Math.floor(t / 600);
    const s = Math.floor((t % 600) / 10);
    return `${m}:${String(s).padStart(2, "0")},${t % 10}`;
  }

  function render() {
    const a = (start / duration) * 100;
    const b = (end / duration) * 100;
    startHandle.style.left = `${a}%`;
    endHandle.style.left = `${b}%`;
    cutLeft.style.width = `${a}%`;
    cutRight.style.width = `${100 - b}%`;
    startHandle.setAttribute("aria-valuetext", formatTime(start));
    endHandle.setAttribute("aria-valuetext", formatTime(end));
    for (const [handle, t] of [[startHandle, start], [endHandle, end]]) {
      const bubble = handle.querySelector(".bubble");
      if (bubble) bubble.textContent = formatTime(t);
    }
    if (summary) {
      summary.textContent = `${formatTime(start)} – ${formatTime(end)} · Länge ${formatTime(end - start)}`;
    }
  }

  function writeInputs() {
    // Ungeschnittene Enden bleiben leer -- sonst speicherte "Speichern" eine
    // Marke, die der Admin nie gesetzt hat.
    startInput.value = start > 0 ? formatTime(start) : "";
    endInput.value = end < duration ? formatTime(end) : "";
  }

  function readInputs() {
    const s = parseTime(startInput.value);
    const e = parseTime(endInput.value);
    if (Number.isNaN(s) || Number.isNaN(e)) return;
    const ns = Math.min(s ?? 0, duration);
    const ne = Math.min(e ?? duration, duration);
    if (ne <= ns) return;
    start = ns;
    end = ne;
    render();
  }

  function timeAt(clientX) {
    const rect = wave.getBoundingClientRect();
    const ratio = Math.min(Math.max((clientX - rect.left) / rect.width, 0), 1);
    return ratio * duration;
  }

  const MIN_GAP = 0.1;

  function stopPreview() {
    if (source) {
      try {
        source.stop();
      } catch (err) {
        // schon beendet
      }
      source = null;
    }
    if (!audio.paused) audio.pause();
    cancelAnimationFrame(frame);
    if (playhead) playhead.classList.remove("on");
  }

  function followPlayhead(position, until) {
    if (!playhead) return;
    playhead.classList.add("on");
    const tick = () => {
      const pos = Math.min(position(), until);
      playhead.style.left = `${(pos / duration) * 100}%`;
      frame = requestAnimationFrame(tick);
    };
    tick();
  }

  function playRange(from, to, showHead) {
    stopPreview();
    from = Math.max(0, from);
    to = Math.min(duration, to);
    if (to - from < 0.05) return;
    if (buffer && audioCtx) {
      audioCtx.resume();
      const node = audioCtx.createBufferSource();
      node.buffer = buffer;
      node.connect(audioCtx.destination);
      node.start(0, from, to - from);
      source = node;
      node.onended = () => {
        if (source === node) stopPreview();
      };
      if (showHead) {
        const startedAt = audioCtx.currentTime;
        followPlayhead(() => from + (audioCtx.currentTime - startedAt), to);
      }
      return;
    }
    // Ohne dekodierte Datei: das <audio>-Element spielt den Bereich.
    audio.currentTime = from;
    const stopAt = () => {
      if (audio.currentTime >= to) {
        audio.removeEventListener("timeupdate", stopAt);
        stopPreview();
      }
    };
    audio.addEventListener("timeupdate", stopAt);
    audio.play().catch(() => stopPreview());
    if (showHead) followPlayhead(() => audio.currentTime, to);
  }

  function grain(isStart) {
    // Mithoeren nur mit dekodierter Datei: das <audio>-Element springt zu
    // traege fuer Hoerproben im Takt der Zeigerbewegung.
    if (!buffer || !live || !live.checked) return;
    const now = performance.now();
    if (now - lastGrain < GRAIN_EVERY_MS) return;
    lastGrain = now;
    if (isStart) playRange(start, start + GRAIN, false);
    else playRange(end - GRAIN, end, false);
  }

  function bindHandle(handle, isStart) {
    handle.style.touchAction = "none";
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      handle.classList.add("live");
      // iOS gibt Audio erst nach einer Beruehrung frei.
      if (audioCtx) audioCtx.resume();
      lastGrain = 0;
      grain(isStart);
    });
    const release = () => handle.classList.remove("live");
    handle.addEventListener("pointerup", release);
    handle.addEventListener("pointercancel", release);
    handle.addEventListener("pointermove", (event) => {
      if (!handle.hasPointerCapture(event.pointerId)) return;
      const t = timeAt(event.clientX);
      if (isStart) start = Math.min(t, end - MIN_GAP);
      else end = Math.max(t, start + MIN_GAP);
      start = Math.max(start, 0);
      end = Math.min(end, duration);
      render();
      writeInputs();
      grain(isStart);
    });
    handle.addEventListener("keydown", (event) => {
      const step = event.shiftKey ? 1 : 0.1;
      let delta = 0;
      if (event.key === "ArrowLeft") delta = -step;
      else if (event.key === "ArrowRight") delta = step;
      else return;
      event.preventDefault();
      if (isStart) start = Math.max(0, Math.min(start + delta, end - MIN_GAP));
      else end = Math.min(duration, Math.max(end + delta, start + MIN_GAP));
      render();
      writeInputs();
      lastGrain = 0;
      grain(isStart);
    });
  }

  function drawPeaks(buffer) {
    const width = canvas.clientWidth * (window.devicePixelRatio || 1);
    const height = canvas.clientHeight * (window.devicePixelRatio || 1);
    if (!width || !height) return;
    canvas.width = width;
    canvas.height = height;
    const data = buffer.getChannelData(0);
    const bucket = Math.max(1, Math.floor(data.length / width));
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = getComputedStyle(wave).color || "#2f4a3a";
    for (let x = 0; x < width; x++) {
      let peak = 0;
      const from = x * bucket;
      for (let i = from; i < from + bucket && i < data.length; i++) {
        const v = Math.abs(data[i]);
        if (v > peak) peak = v;
      }
      const h = Math.max(1, peak * height);
      ctx.fillRect(x, (height - h) / 2, 1, h);
    }
  }

  async function loadPeaks() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx || !wave.dataset.audioSrc) return;
    try {
      const response = await fetch(wave.dataset.audioSrc);
      if (!response.ok) return;
      const ctx = new Ctx();
      const decoded = await ctx.decodeAudioData(await response.arrayBuffer());
      // Kontext und Puffer bleiben fuer die Vorschau erhalten.
      audioCtx = ctx;
      buffer = decoded;
      drawPeaks(decoded);
    } catch (err) {
      // Best effort: Griffe funktionieren auch ohne Zeichnung.
    }
  }

  function init() {
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    duration = audio.duration;
    start = 0;
    end = duration;
    wave.hidden = false;
    readInputs();
    render();
    bindHandle(startHandle, true);
    bindHandle(endHandle, false);
    startInput.addEventListener("input", readInputs);
    endInput.addEventListener("input", readInputs);
    if (preview) {
      preview.hidden = false;
      const on = (role, fn) => {
        const button = preview.querySelector(`[data-role='${role}']`);
        if (button) button.addEventListener("click", fn);
      };
      on("play-cut", () => playRange(start, end, true));
      on("play-head", () => playRange(start, Math.min(start + 3, end), true));
      on("play-tail", () => playRange(Math.max(end - 3, start), end, true));
      on("play-stop", stopPreview);
    }
    loadPeaks();
  }

  if (audio.readyState >= 1) init();
  else audio.addEventListener("loadedmetadata", init, { once: true });
})();
