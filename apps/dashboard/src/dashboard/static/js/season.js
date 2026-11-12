// Overview's season curve (DESIGN.md: Season): the book's value per hour as a baseline series around the season's
// first value, in ink above it and muted ink below (green and red belong to long and short only), drawn with Lightweight Charts from
// /overview/season. Equity arrives as strings; it becomes a number only to draw. Redrawn every five minutes.
// attributionLogo is off: the logo injects an inline <style> the CSP blocks; the page links TradingView instead.
"use strict";
(function () {
  var el = document.querySelector("[data-season]");
  if (!el || !window.LightweightCharts) return;
  var charts = window.LightweightCharts;
  var css = getComputedStyle(document.documentElement);
  function token(name) { return css.getPropertyValue("--" + name).trim(); }
  function tint(hex, alpha) {
    var n = parseInt(hex.slice(1), 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + alpha + ")";
  }
  var REFRESH_MS = 5 * 60 * 1000;
  var chart = null;
  var series = null;

  function say(text) {
    if (chart) { chart.remove(); chart = null; series = null; }
    el.textContent = "";
    var p = document.createElement("p");
    p.className = "season__empty";
    p.textContent = text;
    el.appendChild(p);
  }

  function draw(points) {
    var data = points.map(function (p) { return { time: p.time, value: Number(p.value) }; });
    if (!chart) {
      el.textContent = "";
      chart = charts.createChart(el, {
        autoSize: true,
        layout: {
          background: { type: "solid", color: token("surface") },
          textColor: token("faint"),
          fontFamily: token("type-number-family"),
          fontSize: 12,
          attributionLogo: false
        },
        grid: { vertLines: { visible: false }, horzLines: { color: token("rule") } },
        rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
        timeScale: { borderColor: token("rule"), timeVisible: true, secondsVisible: false, fixLeftEdge: true, fixRightEdge: true },
        crosshair: {
          vertLine: { color: token("edge"), labelBackgroundColor: token("text") },
          horzLine: { color: token("edge"), labelBackgroundColor: token("text") }
        },
        handleScroll: false,
        handleScale: false,
        localization: { locale: "en-GB" }
      });
      series = chart.addSeries(charts.BaselineSeries, {
        baseValue: { type: "price", price: data[0].value },
        topLineColor: token("text"),
        topFillColor1: tint(token("text"), 0.12),
        topFillColor2: tint(token("text"), 0.01),
        bottomLineColor: token("muted"),
        bottomFillColor1: tint(token("muted"), 0.01),
        bottomFillColor2: tint(token("muted"), 0.12),
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true
      });
    } else {
      series.applyOptions({ baseValue: { type: "price", price: data[0].value } });
    }
    series.setData(data);
    chart.timeScale().fitContent();
  }

  function load() {
    fetch(el.getAttribute("data-season"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (body) {
        if (!body.points || body.points.length < 2) {
          say("The season curve appears after the book's first two hours.");
          return;
        }
        draw(body.points);
      })
      .catch(function () {
        if (!chart) say("Season curve unavailable: the dashboard did not answer. It retries in five minutes.");
      });
  }

  load();
  window.setInterval(load, REFRESH_MS);
})();
