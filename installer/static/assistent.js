// Installationsassistent: kleine Helfer. Alles geht auch ohne Skript, der
// Server prüft jede Eingabe selbst noch einmal.
(function () {
  "use strict";

  // Bildschirm 2: Einladungscode beim Einfügen prüfen (Server-Aufruf).
  var code = document.querySelector("textarea[data-pruefen]");
  if (code) {
    var weiter = document.querySelector("[data-weiter]");
    var timer = null;
    var pruefen = function () {
      var daten = new FormData();
      daten.append("code", code.value);
      fetch(code.dataset.pruefen, { method: "POST", body: daten, credentials: "same-origin" })
        .then(function (antwort) { return antwort.text(); })
        .then(function (html) {
          var alt = document.getElementById("code-status");
          alt.outerHTML = html;
          var ok = document.getElementById("code-status").dataset.ok === "1";
          code.classList.toggle("is-err", !ok);
          if (ok) {
            code.removeAttribute("aria-invalid");
            code.removeAttribute("aria-describedby");
          } else {
            code.setAttribute("aria-invalid", "true");
            code.setAttribute("aria-describedby", "code-err");
          }
          if (weiter) weiter.disabled = !ok;
        });
    };
    code.addEventListener("input", function () {
      clearTimeout(timer);
      if (!code.value.trim()) return;
      timer = setTimeout(pruefen, 300);
    });
    if (weiter) weiter.disabled = document.getElementById("code-status").dataset.ok !== "1";
  }

  // Bildschirm 4: anderer Anbieter, andere Felder.
  document.querySelectorAll("input[data-anbieter]").forEach(function (radio) {
    radio.addEventListener("change", function () {
      window.location.href = "/mail?anbieter=" + encodeURIComponent(radio.value);
    });
  });

  // Bildschirm 7: Adresse kopieren, Abschließen erst nach dem Haken.
  document.querySelectorAll("[data-kopieren]").forEach(function (knopf) {
    knopf.addEventListener("click", function () {
      navigator.clipboard.writeText(knopf.dataset.kopieren).then(function () {
        knopf.textContent = "Kopiert";
      });
    });
  });
  var haken = document.querySelector("[data-bestaetigen]");
  var abschliessen = document.querySelector("[data-abschliessen]");
  if (haken && abschliessen) {
    var setzen = function () { abschliessen.disabled = !haken.checked; };
    haken.addEventListener("change", setzen);
    setzen();
  }

  // Wartung, Deinstallieren: mit Haken den Instanznamen verlangen; der Knopf
  // wird erst aktiv, wenn er genau stimmt.
  var loeschen = document.querySelector("[data-loeschen]");
  if (loeschen) {
    var feld = document.querySelector("[data-loeschen-feld]");
    var instanz = document.querySelector("[data-instanz]");
    var entfernen = document.querySelector("[data-deinstallieren]");
    var anpassen = function () {
      feld.hidden = !loeschen.checked;
      loeschen.closest("label").classList.toggle("wz-danger", loeschen.checked);
      entfernen.textContent = loeschen.checked ? "Alles löschen und deinstallieren" : "Deinstallieren";
      entfernen.disabled = loeschen.checked && instanz.value !== instanz.dataset.instanz;
    };
    loeschen.addEventListener("change", anpassen);
    instanz.addEventListener("input", anpassen);
    anpassen();
  }
})();
