// M1 (Spec Advent-Design): das Tuerchen der Karte oeffnet sich, dann geht es zur Aufnahme.
// Ohne JS, bei reduzierter Bewegung oder Modifier-Klick sofort normale Navigation.
(function () {
  const ruhig = window.matchMedia("(prefers-reduced-motion: reduce)");
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-tuerchen-open]");
    if (!link || ruhig.matches) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const tuer = link.closest(".row-story")?.querySelector(".tuerchen");
    if (!tuer) return;
    event.preventDefault();
    let weiter = false;
    const go = () => {
      if (weiter) return;
      weiter = true;
      window.location.href = link.href;
    };
    tuer.querySelector(".klappe").addEventListener("transitionend", go, { once: true });
    setTimeout(go, 900);
    tuer.classList.remove("is-fuge", "is-spalt");
    tuer.classList.add("is-offen");
  });
  // Zurueck-Taste (bfcache): Klappen wieder schliessen.
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    document.querySelectorAll(".tuerchen.is-offen[data-zu]").forEach((t) => {
      t.classList.remove("is-offen");
      t.classList.add(t.dataset.zu);
    });
  });
})();
