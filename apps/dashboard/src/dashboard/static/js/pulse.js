// The heartbeat tape (DESIGN.md: Pulse). Once a second it records the trader's data age from the
// top bar's live partial and draws the last 120 seconds: tick height is the age, its color the
// stale level, and a gap where the link is not live. Nothing is invented: no age, no tick.
"use strict";
(function () {
  var SLOTS = 120;
  var MIN_H = 6;

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim();
  }

  // { age: seconds or null, level: ok|warn|bad } for this second, or null for a gap.
  function sample() {
    var state = document.body.dataset.link;
    if (state !== "live") return null;
    var age = document.querySelector(".topbar__age .age");
    if (!age) return null;
    var level = (age.className.match(/age--(ok|warn|bad|lost)/) || [])[1];
    if (!level || level === "lost") return null;
    var num = age.querySelector(".num");
    var seconds = num ? parseInt(num.textContent, 10) : NaN;
    return { age: isNaN(seconds) ? null : seconds, level: level };
  }

  function parseSample(text) {
    return text.split(",").map(function (part) {
      var n = parseInt(part, 10);
      if (isNaN(n) || n < 0) return null;
      return { age: n, level: "ok" };
    });
  }

  function draw(canvas, ticks, amber, red) {
    var ratio = window.devicePixelRatio || 1;
    var width = canvas.clientWidth;
    var height = canvas.clientHeight;
    if (!width || !height) return;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    var ctx = canvas.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    var colors = { ok: token("muted"), warn: token("warn"), bad: token("loss"), now: token("text") };
    var step = width / SLOTS;
    var bar = Math.max(1, Math.min(3, step - 2));
    var scale = red * 1.5;
    var offset = SLOTS - ticks.length;

    // Baseline: a hairline, dashed under every gap.
    ctx.lineWidth = 1;
    for (var s = 0; s < SLOTS; s++) {
      var tick = s < offset ? undefined : ticks[s - offset];
      ctx.strokeStyle = token(tick === null ? "edge" : "rule");
      ctx.setLineDash(tick === null ? [2, 2] : []);
      ctx.beginPath();
      ctx.moveTo(s * step, height - 0.5);
      ctx.lineTo((s + 1) * step, height - 0.5);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    ticks.forEach(function (tick, i) {
      if (!tick) return;
      var slot = offset + i;
      var level = tick.age === null ? "bad" : tick.age > red ? "bad" : tick.age > amber ? "warn" : tick.level;
      var age = tick.age === null ? scale : Math.min(tick.age, scale);
      // Square root scale: a steady 1 s age still reads as a beat, a stale spike still fills the band.
      var h = MIN_H + (height - MIN_H - 2) * Math.sqrt(age / scale);
      var newest = i === ticks.length - 1;
      ctx.globalAlpha = level === "ok" && !newest ? 0.35 + 0.65 * (slot / SLOTS) : 1;
      ctx.fillStyle = newest && level === "ok" ? colors.now : colors[level];
      ctx.fillRect(Math.round(slot * step + (step - bar) / 2), Math.round(height - 1 - h), bar, Math.round(h));
    });
    ctx.globalAlpha = 1;
  }

  function mount(canvas) {
    var amber = parseFloat(canvas.dataset.amber) || 3;
    var red = parseFloat(canvas.dataset.red) || 10;
    var fixed = canvas.dataset.sampleTicks;
    var ticks = fixed ? parseSample(fixed) : [];
    var redraw = function () { draw(canvas, ticks, amber, red); };
    if (window.ResizeObserver) new ResizeObserver(redraw).observe(canvas);
    redraw();
    if (fixed) return;
    window.setInterval(function () {
      ticks.push(sample());
      if (ticks.length > SLOTS) ticks.shift();
      redraw();
    }, 1000);
  }

  function start() {
    document.querySelectorAll("canvas[data-pulse]").forEach(mount);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
