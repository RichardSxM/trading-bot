// Lightweight Charts, themed from the design tokens (spec 0004, DESIGN.md: Chart).
// Each [data-chart] element carries its candles in data-series; prices arrive as strings.
// attributionLogo is off: the logo injects an inline <style> the CSP blocks, so the
// template shows a "Charting by TradingView" text link instead.
"use strict";
(function () {
  if (!window.LightweightCharts) return;
  var charts = window.LightweightCharts;
  var css = getComputedStyle(document.documentElement);
  function token(name) { return css.getPropertyValue("--" + name).trim(); }

  document.querySelectorAll("[data-chart]").forEach(function (el) {
    var candles;
    try {
      candles = JSON.parse(el.getAttribute("data-series") || "[]").map(function (c) {
        return { time: c.time, open: Number(c.open), high: Number(c.high), low: Number(c.low), close: Number(c.close) };
      });
    } catch (err) {
      el.textContent = "Chart data could not be read.";
      return;
    }
    var chart = charts.createChart(el, {
      autoSize: true,
      layout: {
        background: { type: "solid", color: token("bg") },
        textColor: token("muted"),
        fontFamily: token("type-number-family"),
        fontSize: 12,
        attributionLogo: false
      },
      grid: { vertLines: { color: token("rule") }, horzLines: { color: token("rule") } },
      rightPriceScale: { borderColor: token("rule") },
      timeScale: { borderColor: token("rule"), timeVisible: true, secondsVisible: false },
      crosshair: {
        vertLine: { color: token("edge"), labelBackgroundColor: token("raised") },
        horzLine: { color: token("edge"), labelBackgroundColor: token("raised") }
      },
      localization: { locale: "en-GB" }
    });
    var series = chart.addSeries(charts.CandlestickSeries, {
      upColor: token("profit"), downColor: token("loss"),
      wickUpColor: token("profit"), wickDownColor: token("loss"),
      borderVisible: false
    });
    series.setData(candles);
    chart.timeScale().fitContent();
  });
})();
