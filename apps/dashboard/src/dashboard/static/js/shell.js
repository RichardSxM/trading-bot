// Shell behaviour (spec 0004). Hardens htmx, gives the stop button its error states (AC-13),
// and maps the SSE link to the link status: live, reconnecting after 5 s of silence, lost after
// 15 s, with the age and command dimmed and labelled while it is not live (AC-10).
"use strict";
(function () {
  if (!window.htmx) return;
  var htmx = window.htmx;
  htmx.config.allowEval = false;
  htmx.config.includeIndicatorStyles = false;
  // The stop target shows its own 401, 403, and 503 states (AC-13); other errors swap nothing.
  htmx.config.responseHandling = [
    { code: "204", swap: false },
    { code: "[23]..", swap: true },
    { code: "401|403|503", swap: true, error: false },
    { code: "[45]..", swap: false, error: true },
    { code: "...", swap: false }
  ];

  // The request never reached the dashboard: say the stop was NOT sent and offer a retry.
  document.addEventListener("htmx:sendError", function (event) {
    var form = event.detail.elt && event.detail.elt.closest("#stop-area");
    var failed = document.getElementById("stop-failed");
    if (!form || !failed) return;
    var old = form.querySelector(".stop-area__error");
    if (old) old.remove();
    form.classList.add("stop-area--failed");
    form.insertBefore(failed.content.cloneNode(true), form.querySelector("button"));
    var label = form.querySelector(".button__label");
    if (label) label.textContent = "Retry stop";
  });

  // ---- Link status ----
  var RECONNECTING_AFTER_MS = 5000;
  var LOST_AFTER_MS = 15000;
  var EVENTS = ["ping", "topbar-age", "topbar-command"];
  var link = document.getElementById("link-status");
  if (!link) return;
  var text = link.querySelector(".link__text");
  var lastEvent = 0;
  var probing = false;

  function show(state) {
    if (link.dataset.state === state) return;
    link.dataset.state = state;
    link.className = "link link--" + state;
    text.textContent = state === "connecting" ? "connecting" : state;
    document.body.dataset.link = state;
  }

  function beat() {
    lastEvent = Date.now();
    show("live");
    link.classList.remove("is-beat");
    void link.offsetWidth; // restart the one shot animation
    link.classList.add("is-beat");
  }

  // The stream closed for good: a 204 means the session is gone, so reload to reach the login.
  // Anything else (the dashboard is down) keeps this page and its stop button on screen.
  function probe() {
    if (probing) return;
    probing = true;
    var controller = new AbortController();
    fetch("/events", { signal: controller.signal, cache: "no-store" })
      .then(function (response) {
        if (response.status === 204) window.location.reload();
      })
      .catch(function () {})
      .finally(function () {
        controller.abort();
        probing = false;
      });
  }

  document.body.addEventListener("htmx:sseOpen", function (event) {
    var source = event.detail.source;
    EVENTS.forEach(function (name) { source.addEventListener(name, beat); });
    beat();
  });
  document.body.addEventListener("htmx:sseError", function (event) {
    var source = event.detail.source;
    if (source && source.readyState === EventSource.CLOSED) probe();
  });

  show("connecting");
  lastEvent = Date.now();
  window.setInterval(function () {
    var silent = Date.now() - lastEvent;
    if (silent > LOST_AFTER_MS) show("lost");
    else if (silent > RECONNECTING_AFTER_MS) show("reconnecting");
  }, 1000);
})();
