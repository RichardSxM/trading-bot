// Overview's standings (DESIGN.md: Standings row): after each live swap, a coin whose rank, side, or near or flip
// state changed flashes its row once. Price ticks alone never flash; the first render sets the baseline.
"use strict";
(function () {
  var host = document.querySelector(".standings-live");
  if (!host) return;
  var seen = {};
  function read(flash) {
    host.querySelectorAll("tr[data-coin]").forEach(function (row) {
      var coin = row.getAttribute("data-coin");
      var state = row.getAttribute("data-state");
      if (flash && coin in seen && seen[coin] !== state) row.classList.add("is-changed");
      seen[coin] = state;
    });
  }
  read(false);
  document.body.addEventListener("htmx:afterSettle", function (event) {
    if (event.target === host || host.contains(event.target)) read(true);
  });
})();
