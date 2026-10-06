/* Aufnahme-Client (U7). Einzige nennenswerte Client-Logik der App.
 *
 * KTD7: startet ohne Formatvorgabe, der clientseitig gemeldete Typ geht
 * nur als Diagnosefeld mit, nie in die Formatentscheidung -- die trifft
 * ffmpeg serverseitig aus dem Inhalt (app/recording/normalize.py).
 * KTD8: Mikrofon-Berechtigung wird ueber den tatsaechlichen Fehler des
 * getUserMedia-Aufrufs erkannt, nicht ueber die Permissions-API (auf iOS
 * bewusst unzuverlaessig: liefert "prompt" auch bei dauerhafter Ablehnung).
 */
(function () {
  "use strict";

  const root = document.getElementById("recorder-app");
  if (!root) return;

  const submitUrl = root.dataset.submitUrl;

  const states = {};
  root.querySelectorAll("[data-state]").forEach((el) => {
    states[el.dataset.state] = el;
  });

  function showState(name) {
    Object.values(states).forEach((el) => {
      el.hidden = true;
    });
    if (states[name]) states[name].hidden = false;
  }

  // --- Vorschlag vorlesen / Eigene Geschichte (R24, AE17, AE18) ---
  const choiceButtons = root.querySelectorAll("[data-choice]");
  const storyWrap = root.querySelector("[data-role='story-wrap']");
  const titleField = root.querySelector("[data-role='title-field']");
  choiceButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      choiceButtons.forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      const own = btn.dataset.choice === "own";
      if (storyWrap) storyWrap.hidden = own;
      if (titleField) titleField.hidden = !own;
    });
  });

  // --- R47: Auto-Scroll mit einstellbarer Geschwindigkeit + Schriftgroesse.
  // Wirkt ausschliesslich auf die Textdarstellung, nie auf die Aufnahme. ---
  const storyScroll = root.querySelector("[data-role='story-scroll']");
  const storyText = root.querySelector("[data-role='story-text']");
  const speedSlider = root.querySelector("[data-role='scroll-speed']");
  const fontSlider = root.querySelector("[data-role='font-size']");
  // Stufenlos in Pixel pro Sekunde (Regler 0 bis max im Template). Die
  // Position wird als Kommazahl mitgefuehrt: Browser runden scrollTop auf
  // ganze Pixel, kleine Schritte pro Frame gingen sonst verloren und der
  // Text stand bei niedrigen Werten still.
  let scrollPos = 0;
  let lastFrame = null;

  function scrollTick(now) {
    const speed = Number(speedSlider.value) || 0;
    if (lastFrame !== null && speed > 0) {
      // Von Hand gescrollt: dort weitermachen.
      if (Math.abs(storyScroll.scrollTop - scrollPos) > 2) scrollPos = storyScroll.scrollTop;
      scrollPos += (speed * Math.min(now - lastFrame, 100)) / 1000;
      storyScroll.scrollTop = scrollPos;
    } else {
      scrollPos = storyScroll.scrollTop;
    }
    lastFrame = now;
    requestAnimationFrame(scrollTick);
  }
  if (storyScroll && speedSlider) {
    requestAnimationFrame(scrollTick);
  }
  if (fontSlider && storyText) {
    fontSlider.addEventListener("input", () => {
      storyText.style.fontSize = fontSlider.value + "px";
    });
  }

  // --- Faehigkeitspruefung: Berechtigung verweigert (R9/KTD8), eingebetteter
  // Mail-Browser (R38), oder gar keine Aufnahmefaehigkeit (Sackgasse). ---
  const deniedReasonEl = root.querySelector("[data-role='denied-reason']");
  const deniedStepsEl = root.querySelector("[data-role='denied-steps']");

  function isLikelyEmbeddedBrowser() {
    const ua = navigator.userAgent || "";
    // UA-Erkennung ist hier nur ein Hinweis, kein Beweis (dieselbe
    // Philosophie wie KTD7/KTD8: die Faehigkeit selbst wird ueber den
    // tatsaechlichen Aufruf geprueft) -- sie entscheidet ausschliesslich,
    // welcher der beiden Sackgassen-Texte erscheint, nie ob ueberhaupt
    // einer erscheint.
    return /FBAN|FBAV|Instagram|Line\/|MicroMessenger|GSA\/|OutlookMobile/i.test(ua);
  }

  function isIOS() {
    return /iPhone|iPad|iPod/i.test(navigator.userAgent || "");
  }

  function showDenied(reasonText) {
    if (deniedReasonEl) deniedReasonEl.textContent = reasonText;
    if (deniedStepsEl) {
      deniedStepsEl.innerHTML = "";
      const steps = isIOS()
        ? [
            "Einstellungen öffnen",
            "Safari (oder deine Browser-App) auswählen",
            "Mikrofon erlauben",
            "Diese Seite neu laden",
          ]
        : [
            "Auf das Schloss-Symbol in der Adresszeile tippen",
            "Berechtigungen öffnen",
            "Mikrofon erlauben",
            "Diese Seite neu laden",
          ];
      steps.forEach((text) => {
        const li = document.createElement("li");
        li.textContent = text;
        deniedStepsEl.appendChild(li);
      });
    }
    showState("denied");
  }

  const copyValueEl = root.querySelector("[data-role='copy-link-value']");
  const copyButtonEl = root.querySelector("[data-role='copy-link-button']");
  if (copyValueEl) copyValueEl.textContent = window.location.href;
  if (copyButtonEl) {
    copyButtonEl.addEventListener("click", () => {
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(window.location.href).then(() => {
        copyButtonEl.textContent = "Kopiert";
        setTimeout(() => {
          copyButtonEl.textContent = "Kopieren";
        }, 1500);
      });
    });
  }

  // Diagnose-Block fuer den Sackgassen-/eingebetteter-Browser-Zustand:
  // faktische Werte statt Ratens, wenn ein reales Geraet unerwartet dort
  // landet (Debugging-Hilfe fuer die Real-Geraet-Verifikation, kein
  // Produktfeature -- deshalb standardmaessig eingeklappt).
  function renderDebugInfo() {
    const lines = [
      "protocol=" + location.protocol,
      "isSecureContext=" + window.isSecureContext,
      "navigator.mediaDevices=" + Boolean(navigator.mediaDevices),
      "getUserMedia=" +
        typeof (navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      "MediaRecorder=" + typeof MediaRecorder,
      "userAgent=" + navigator.userAgent,
    ];
    root.querySelectorAll("[data-role='debug-info']").forEach((el) => {
      el.textContent = lines.join("\n");
    });
  }

  function hasRecordingCapability() {
    return Boolean(
      navigator.mediaDevices &&
        typeof navigator.mediaDevices.getUserMedia === "function" &&
        typeof MediaRecorder !== "undefined"
    );
  }

  // --- Bildschirmsperre waehrend der Aufnahme (Approach Schritt 4) ---
  let wakeLock = null;
  async function requestWakeLock() {
    try {
      if ("wakeLock" in navigator) {
        wakeLock = await navigator.wakeLock.request("screen");
      }
    } catch (err) {
      /* best effort -- kein Abbruch der Aufnahme deswegen */
    }
  }
  function releaseWakeLock() {
    if (wakeLock) {
      wakeLock.release().catch(() => {});
      wakeLock = null;
    }
  }

  // --- Aufnahme selbst ---
  let mediaRecorder = null;
  let mediaStream = null;
  let chunks = [];
  let recordedBlob = null;
  let reportedType = "";
  let startedAt = 0;
  let timerHandle = null;
  let wasInterrupted = false;
  let hasPlayedToEnd = false;
  let previewAudio = null;
  let previewUrl = null;

  const recordBtn = root.querySelector("[data-role='record-btn']");
  const stopBtn = root.querySelector("[data-role='stop-btn']");
  const playButtons = root.querySelectorAll("[data-role='play-btn']");
  const rerecordButtons = root.querySelectorAll("[data-role='rerecord-btn']");
  // Zwei Zustaende (review/interrupted) tragen je einen eigenen Absenden-
  // Knopf mit demselben data-role -- beide muessen verkabelt und im
  // Sperr-Zustand synchron gehalten werden (Review-Fund: mit einem
  // einzelnen querySelector blieb der im Unterbrechungs-Zustand sichtbare
  // Knopf dauerhaft ohne Klick-Handler und dauerhaft deaktiviert).
  const submitBtns = root.querySelectorAll("[data-role='submit-btn']");
  const retryBtn = root.querySelector("[data-role='retry-btn']");
  const timerEls = root.querySelectorAll("[data-role='timer']");
  const progressFill = root.querySelector("[data-role='progress-fill']");
  let uploadInFlight = false;
  let startingRecording = false;

  // M2 (Spec Advent-Design): ruhiger Lichtkranz im Takt der Stimme. Nur Darstellung;
  // scheitert irgendetwas, atmet der Kranz per CSS weiter und die Aufnahme laeuft.
  const halo = root.querySelector(".rec-halo");
  const pegel = { ctx: null, frame: null, lvl: 0, last: 0 };

  function pegelVorbereiten() {
    // In der Klick-Geste erzeugen, sonst startet iOS den Kontext suspendiert.
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (halo && Ctx && !reducedMotion) pegel.ctx = new Ctx();
    } catch (_) {
      pegel.ctx = null;
    }
  }

  function pegelStarten(stream) {
    if (!pegel.ctx) return;
    try {
      // iOS laesst den Kontext nach dem await in startRecording() manchmal
      // suspendiert -- ohne resume() liefert der Analyser dauerhaft Nullen.
      if (pegel.ctx.state !== "running") pegel.ctx.resume().catch(() => {});
      const analyser = pegel.ctx.createAnalyser();
      analyser.fftSize = 1024;
      pegel.ctx.createMediaStreamSource(stream).connect(analyser);
      const data = new Float32Array(analyser.fftSize);
      pegel.last = performance.now();
      const tick = (now) => {
        // Die CSS-Atmung bleibt aktiv, bis der Kontext wirklich laeuft --
        // sonst friert der Kranz auf "ausgeschaltet" ein, solange er noch
        // suspendiert ist.
        if (pegel.ctx.state === "running") {
          halo.classList.add("hat-pegel");
        } else {
          halo.classList.remove("hat-pegel");
        }
        analyser.getFloatTimeDomainData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
        const ziel = Math.min(1, Math.sqrt(sum / data.length) * 6);
        const dt = now - pegel.last;
        pegel.last = now;
        const tau = ziel > pegel.lvl ? 250 : 900;
        pegel.lvl += (ziel - pegel.lvl) * (1 - Math.exp(-dt / tau));
        halo.style.setProperty("--lvl", pegel.lvl.toFixed(3));
        pegel.frame = requestAnimationFrame(tick);
      };
      pegel.frame = requestAnimationFrame(tick);
    } catch (_) {
      pegelStoppen();
    }
  }

  function pegelStoppen() {
    if (pegel.frame) cancelAnimationFrame(pegel.frame);
    pegel.frame = null;
    if (pegel.ctx) pegel.ctx.close().catch(() => {});
    pegel.ctx = null;
    pegel.lvl = 0;
    if (halo) {
      halo.classList.remove("hat-pegel");
      halo.style.removeProperty("--lvl");
    }
  }

  function formatTime(ms) {
    const total = Math.floor(ms / 1000);
    const m = String(Math.floor(total / 60)).padStart(2, "0");
    const s = String(total % 60).padStart(2, "0");
    return m + ":" + s;
  }

  function tickTimer() {
    const el = formatTime(Date.now() - startedAt);
    timerEls.forEach((node) => {
      node.textContent = el;
    });
  }

  async function startRecording() {
    // Review-Fund: ohne diese Sperre startet ein schnelles Doppeltippen auf
    // "Aufnehmen" zwei ueberlappende getUserMedia-Anfragen; loest die
    // zweite frueher auf als die erste, ueberschreibt sie mediaStream und
    // mediaRecorder mitten in der Einrichtung der ersten.
    if (startingRecording) return;
    startingRecording = true;
    pegelVorbereiten();
    if (recordBtn) recordBtn.disabled = true;

    if (!hasRecordingCapability()) {
      startingRecording = false;
      pegelStoppen();
      if (recordBtn) recordBtn.disabled = false;
      renderDebugInfo();
      if (isLikelyEmbeddedBrowser()) {
        showState("embedded-browser");
      } else {
        showState("dead-end");
      }
      return;
    }

    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      startingRecording = false;
      pegelStoppen();
      if (recordBtn) recordBtn.disabled = false;
      const name = err && err.name;
      if (name === "NotFoundError") {
        showDenied("Für dieses Gerät wurde kein Mikrofon gefunden.");
      } else if (name === "NotReadableError") {
        showDenied("Das Mikrofon wird gerade von einer anderen App verwendet.");
      } else {
        showDenied("Dieses Gerät hat den Mikrofonzugriff bisher verweigert.");
      }
      return;
    }

    startingRecording = false;
    if (recordBtn) recordBtn.disabled = false;
    chunks = [];
    wasInterrupted = false;
    hasPlayedToEnd = false;

    // KTD7: kein mimeType erzwungen -- der Browser liefert, was er kann.
    mediaRecorder = new MediaRecorder(mediaStream);
    mediaRecorder.addEventListener("dataavailable", (event) => {
      if (event.data && event.data.size > 0) chunks.push(event.data);
    });
    mediaRecorder.addEventListener("stop", onRecordingStopped);
    mediaRecorder.start();
    pegelStarten(mediaStream);

    startedAt = Date.now();
    timerHandle = setInterval(tickTimer, 200);
    requestWakeLock();
    showState("recording");
  }

  function stopRecording(interrupted) {
    pegelStoppen();
    wasInterrupted = Boolean(interrupted);
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      mediaRecorder.stop();
    }
    if (timerHandle) {
      clearInterval(timerHandle);
      timerHandle = null;
    }
    if (mediaStream) {
      mediaStream.getTracks().forEach((track) => track.stop());
      mediaStream = null;
    }
  }

  function onRecordingStopped() {
    reportedType = (mediaRecorder && mediaRecorder.mimeType) || (chunks[0] && chunks[0].type) || "";
    recordedBlob = new Blob(chunks, { type: reportedType || "application/octet-stream" });
    releaseWakeLock();
    setupPreview();
    showState(wasInterrupted ? "interrupted" : "review");
  }

  function setupPreview() {
    if (previewAudio) previewAudio.pause();
    // Review-Fund: jede neue Aufnahme (auch "Neu aufnehmen") erzeugte hier
    // bisher eine weitere Object-URL, ohne die vorherige je freizugeben.
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    hasPlayedToEnd = false;
    previewUrl = URL.createObjectURL(recordedBlob);
    previewAudio = new Audio(previewUrl);
    previewAudio.addEventListener("ended", () => {
      hasPlayedToEnd = true;
      updateSubmitGate();
    });
    updateSubmitGate();
  }

  function updateSubmitGate() {
    // R31: nach einer Unterbrechung erst nach vollstaendigem Anhoeren.
    // Nur einer der beiden Knoepfe ist je sichtbar (review xor interrupted
    // state), aber wasInterrupted gilt fuer beide gleich: im review-
    // Zustand ist es stets false (disabled=false), im interrupted-Zustand
    // richtet es sich nach hasPlayedToEnd -- ein einheitliches Toggle
    // ueber beide Knoten ist also fuer beide Zustaende korrekt.
    submitBtns.forEach((btn) => {
      btn.disabled = wasInterrupted && !hasPlayedToEnd;
    });
  }

  function playPreview() {
    if (previewAudio) {
      previewAudio.currentTime = 0;
      previewAudio.play();
    }
  }

  function resetRecording() {
    recordedBlob = null;
    wasInterrupted = false;
    hasPlayedToEnd = false;
    showState("idle");
  }

  function currentTitle() {
    if (!titleField || titleField.hidden) return "";
    const input = titleField.querySelector("input[name='title']");
    return input ? input.value : "";
  }

  function submitRecording() {
    // Review-Fund: ein Doppelklick auf "Abschicken" (oder "Erneut
    // versuchen" kurz nach einem fehlgeschlagenen Versuch) loeste bisher
    // zwei parallele XHR-Uploads derselben Aufnahme aus.
    if (!recordedBlob || uploadInFlight) return;
    uploadInFlight = true;

    const form = new FormData();
    const extension = /mp4/.test(reportedType) ? "m4a" : "webm";
    form.append("audio", recordedBlob, "aufnahme." + extension);
    form.append("reported_type", reportedType);
    form.append("title", currentTitle());

    const xhr = new XMLHttpRequest();
    xhr.open("POST", submitUrl);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && progressFill) {
        const pct = Math.round((event.loaded / event.total) * 100);
        progressFill.style.width = pct + "%";
      }
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 200) {
        const data = JSON.parse(xhr.responseText);
        window.location.href = data.next_url;
        return;
      }
      showFailed(xhr);
    });
    xhr.addEventListener("error", () => showFailed(null));

    showState("uploading");
    if (progressFill) progressFill.style.width = "0%";
    xhr.send(form);
  }

  function showFailed(xhr) {
    uploadInFlight = false;
    let message = "Die Verbindung ist abgebrochen.";
    if (xhr) {
      message = "Die Übertragung ist fehlgeschlagen.";
      try {
        const data = JSON.parse(xhr.responseText);
        if (data && data.error) message = data.error;
      } catch (err) {
        /* Server antwortete nicht mit JSON -- Standardtext bleibt. */
      }
    }
    const reasonEl = root.querySelector("[data-role='failed-reason']");
    if (reasonEl) reasonEl.textContent = message;
    showState("failed");
  }

  if (recordBtn) recordBtn.addEventListener("click", startRecording);
  if (stopBtn) stopBtn.addEventListener("click", () => stopRecording(false));
  playButtons.forEach((btn) => btn.addEventListener("click", playPreview));
  rerecordButtons.forEach((btn) => btn.addEventListener("click", resetRecording));
  submitBtns.forEach((btn) => btn.addEventListener("click", submitRecording));
  if (retryBtn) retryBtn.addEventListener("click", submitRecording);

  // R31: Unterbrechung durch Sichtbarkeitswechsel (Bildschirmsperre, Anruf,
  // App-Wechsel) erkennen und die Sperre danach neu anfordern.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (mediaRecorder && mediaRecorder.state === "recording") {
        stopRecording(true);
      }
    } else if (mediaRecorder && mediaRecorder.state === "recording" && !wakeLock) {
      requestWakeLock();
    }
  });

  if (!hasRecordingCapability()) renderDebugInfo();
  showState(hasRecordingCapability() ? "idle" : isLikelyEmbeddedBrowser() ? "embedded-browser" : "dead-end");
})();
