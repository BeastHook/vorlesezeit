// Knopf-Rueckmeldung und Live-Status (Mockup docs/design/
// feedback-live-status-mockup.html, abgenommen 2026-10-03).
//
// 1. Beim Absenden wird der Knopf grau (`is-pending`) mit kurzem Text aus
//    `data-busy` (Knopf oder Formular, sonst "Speichert …") und laesst sich
//    nicht doppelt ausloesen. Der Knopf wird in sessionStorage gemerkt; nach
//    dem Neuladen faerbt sich genau dieser Knopf gruen oder rot, je nachdem,
//    ob die neue Seite ein `[data-result=ok]` oder `[data-result=err]` traegt
//    (Hinweisbalken, Pruefhinweis), und blendet zurueck. Landet die Antwort auf
//    einer anderen Seite oder ohne Ergebnis, passiert nichts.
// 2. Solange im Verlauf ein Lauf "laeuft" bzw. im Setup ein Tonie gesperrt
//    ist, fragt die Seite alle 3 s eine kleine Statusadresse ab.
(() => {
  const STORE = "admin-feedback";
  const POLL_MS = 3000;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- 1. Knopf-Rueckmeldung ---------------------------------------------------

  function path(url) {
    const parsed = new URL(url, location.href);
    return parsed.pathname + parsed.search;
  }

  // `button.formAction` liefert ohne eigenes formaction-Attribut die Adresse
  // der Seite, nicht das action des Formulars -- daher selbst aufloesen.
  function actionOf(el) {
    const raw = el.getAttribute("formaction") || el.form.getAttribute("action") || location.href;
    return new URL(raw, location.href).href;
  }

  function keyOf(el) {
    if (el.tagName === "A") return "a " + path(el.href);
    const form = el.form;
    const parts = [path(actionOf(el)), el.name, el.value];
    form.querySelectorAll("input[type=hidden]").forEach((input) => {
      parts.push(input.name + "=" + input.value);
    });
    return parts.join("|");
  }

  function candidates() {
    return [...document.querySelectorAll("button.btn[type=submit], a.btn[data-busy]")].filter(
      (el) => el.tagName === "A" || (el.form && el.form.method === "post"),
    );
  }

  function remember(el, target) {
    const key = keyOf(el);
    const index = candidates()
      .filter((other) => keyOf(other) === key)
      .indexOf(el);
    try {
      sessionStorage.setItem(
        STORE,
        JSON.stringify({ key, index, from: location.pathname, to: new URL(target, location.href).pathname, at: Date.now() }),
      );
    } catch (e) {
      // ohne sessionStorage nur der graue Zustand
    }
  }

  function setPending(el, holder) {
    el.classList.remove("is-ok", "is-err", "is-fading");
    el.classList.add("is-pending");
    el.textContent = el.dataset.busy || (holder && holder.dataset.busy) || "Speichert …";
  }

  document.addEventListener("submit", (event) => {
    const form = event.target;
    const button = event.submitter;
    if (event.defaultPrevented || form.method !== "post") return;
    if (form.dataset.sent) {
      event.preventDefault();
      return;
    }
    form.dataset.sent = "1";
    if (!button || !button.classList.contains("btn")) return;
    remember(button, actionOf(button));
    setPending(button, form);
    // Erst nach dem Absenden sperren, sonst fehlt name/value des Knopfs.
    setTimeout(() => {
      button.disabled = true;
    });
    // Felder nur schreibgeschuetzt: gesperrte Felder schickt der Browser nicht mit.
    form.querySelectorAll("input:not([type=hidden])").forEach((input) => {
      input.readOnly = true;
    });
    if (form.dataset.busyNote) {
      const note = document.createElement("p");
      note.className = "meta";
      note.setAttribute("role", "status");
      note.textContent = form.dataset.busyNote;
      form.appendChild(note);
    }
  });

  document.addEventListener("click", (event) => {
    const link = event.target.closest("a.btn[data-busy]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey) return;
    if (link.classList.contains("is-pending")) {
      event.preventDefault();
      return;
    }
    remember(link, link.href);
    setPending(link);
  });

  // Zurueck-Navigation aus dem Seitencache: kein grauer Knopf, der nie endet.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && document.querySelector(".is-pending")) location.reload();
  });

  function showResult() {
    let saved = null;
    try {
      saved = JSON.parse(sessionStorage.getItem(STORE));
      sessionStorage.removeItem(STORE);
    } catch (e) {
      return;
    }
    if (!saved || Date.now() - saved.at > 120000) return;
    if (location.pathname !== saved.from && location.pathname !== saved.to) return;
    const failed = document.querySelector("[data-result=err]");
    const result = failed ? "err" : document.querySelector("[data-result=ok]") ? "ok" : null;
    if (!result) return;
    const matches = candidates().filter((el) => keyOf(el) === saved.key);
    const el = matches[saved.index] || matches[0];
    if (!el) return;
    // Eine Fehlerseite aus dem POST beginnt oben; Knopf und Meldung lagen dann
    // ausserhalb des Blicks.
    const box = el.getBoundingClientRect();
    if (box.top < 0 || box.bottom > window.innerHeight) {
      el.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
    }
    const label = el.textContent;
    el.textContent = result === "ok" ? el.dataset.done || "Gespeichert ✓" : "Fehlgeschlagen ✕";
    el.classList.add(result === "ok" ? "is-ok" : "is-err");
    const restore = () => {
      el.textContent = label;
      el.classList.remove("is-fading");
    };
    if (reduced) {
      setTimeout(() => {
        el.classList.remove("is-ok", "is-err");
        restore();
      }, 1500);
      return;
    }
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        el.classList.add("is-fading");
        el.classList.remove("is-ok", "is-err");
      }),
    );
    setTimeout(restore, 1600);
  }

  // --- 2. Live-Status ------------------------------------------------------------

  function poll(url, handle) {
    const tick = async () => {
      let data;
      try {
        const response = await fetch(url(), { headers: { Accept: "application/json" } });
        if (!response.ok) return;
        data = await response.json();
      } catch (e) {
        return; // abgemeldet oder offline: nicht endlos weiterfragen
      }
      if (handle(data)) setTimeout(tick, POLL_MS);
    };
    setTimeout(tick, POLL_MS);
  }

  function flashRow(row) {
    row.classList.remove("is-updated");
    void row.offsetWidth;
    row.classList.add("is-updated");
  }

  const runs = document.querySelector("[data-live-runs]");
  if (runs) {
    const running = () => [...runs.querySelectorAll("tr[data-running]")];
    poll(
      () => runs.dataset.liveRuns + "?ids=" + running().map((row) => row.dataset.runId).join(","),
      (data) => {
        const byId = new Map(data.runs.map((run) => [String(run.id), run]));
        running().forEach((row) => {
          const run = byId.get(row.dataset.runId);
          if (run && run.running) return;
          row.removeAttribute("data-running");
          if (!run) return; // anderer Tonie gewaehlt: nicht weiter fragen
          const outcome = row.querySelector("[data-cell=outcome]");
          outcome.className = run.outcome_class;
          outcome.textContent = run.outcome_text;
          row.querySelector("[data-cell=content]").textContent = run.content;
          flashRow(row);
        });
        if (running().length) return true;
        const hint = runs.querySelector("[data-live-hint]");
        if (hint) hint.hidden = true;
        return false;
      },
    );
  }

  const setup = document.querySelector("[data-live-setup]");
  if (setup) {
    let edited = false;
    document.addEventListener("input", () => {
      edited = true;
    });
    const locked = [...setup.querySelectorAll("[data-busy-tonie]")].map((el) => Number(el.dataset.busyTonie));
    poll(
      () => setup.dataset.liveSetup,
      (data) => {
        if (locked.some((id) => data.busy.includes(id))) return true;
        // Lauf vorbei: entsperrt zeigt die Seite der Server. Eingaben nicht verwerfen.
        if (edited) {
          setup.querySelectorAll(".lock-note").forEach((note) => {
            note.textContent = "Der Lauf ist beendet. Lade die Seite neu, um zu entsperren.";
          });
        } else if (location.pathname === "/admin/setup") {
          location.reload();
        } else {
          location.assign("/admin/setup#s-konten");
        }
        return false;
      },
    );
  }

  // --- 3. Aufnahmen herunterladen ---------------------------------------------
  // Mockup docs/design/aufnahmen-download-mockup.html: "Auswahl" blendet Haekchen
  // ein und wird zu "Download Auswahl"; ein Klick auf die Zeile hakt dann an,
  // statt die Aufnahme zu oeffnen.
  const dl = document.querySelector("[data-dl]");
  const dlForm = document.getElementById("dl-form");
  if (dl && dlForm) {
    const auswahl = dlForm.querySelector('[data-role="auswahl"]');
    const abbrechen = dlForm.querySelector('[data-role="abbrechen"]');
    const alle = dlForm.querySelector('[data-role="dl-alle"]');
    const hinweis = dlForm.querySelector('[data-role="hinweis"]');
    const checks = [...dl.querySelectorAll(".rec-check")];
    let aktiv = false;
    const update = () => {
      const n = checks.filter((c) => c.checked).length;
      dl.classList.toggle("auswahl", aktiv);
      abbrechen.hidden = !aktiv;
      alle.hidden = aktiv;
      auswahl.classList.toggle("btn-secondary", aktiv);
      auswahl.classList.toggle("btn-ghost", !aktiv);
      auswahl.textContent = aktiv ? (n ? `Download Auswahl (${n})` : "Download Auswahl") : "Auswahl";
      auswahl.disabled = aktiv && n === 0;
      hinweis.textContent = aktiv ? "Aufnahmen anhaken" : hinweis.dataset.alle;
    };
    auswahl.addEventListener("click", () => {
      if (aktiv) dlForm.requestSubmit();
      else aktiv = true;
      update();
    });
    abbrechen.addEventListener("click", () => {
      aktiv = false;
      checks.forEach((c) => (c.checked = false));
      update();
    });
    checks.forEach((check) => {
      check.closest(".rec-row").addEventListener("click", (event) => {
        if (!aktiv || event.target.closest(".btn")) return;
        event.preventDefault();
        check.checked = !check.checked;
        update();
      });
    });
    update();
  }

  showResult();
})();
