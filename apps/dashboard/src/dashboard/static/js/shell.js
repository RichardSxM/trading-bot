// Shell behaviour (spec 0004). Hardens htmx and gives the stop button its error states
// (AC-13). Link status, the silence watchdog, and link lost dimming arrive with milestone 4.
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
    var button = form.querySelector("button");
    if (button) button.textContent = "Retry stop";
  });
})();
