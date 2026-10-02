// Shell behaviour (spec 0004). Milestone 1: harden htmx only. Link status, the silence
// watchdog, link lost dimming, and the stop error states arrive with later milestones.
"use strict";
(function () {
  if (!window.htmx) return;
  window.htmx.config.allowEval = false;
  window.htmx.config.includeIndicatorStyles = false;
})();
