---
version: 1
slug: "apps-dashboard-src-dashboard-templates-shell-html"
primary_target: "apps/dashboard/src/dashboard/templates/shell.html"
related_targets: ["apps/dashboard/src/dashboard/templates/pages/overview.html","apps/dashboard/src/dashboard/templates/login.html","apps/dashboard/src/dashboard/templates/pages/styleguide.html"]
---

## Scope

The dashboard shell and every page inside it: top bar, side nav and phone tab bar, Overview, Trades, Strategies, System, login, and /styleguide. Mode: Operate.

## Audience and job

One trader supervising an automated Kraken Futures scalper, on a laptop at a desk and a phone in a hand. First read: what just happened. Then: is it fresh, is it DEMO or LIVE, where is the stop.

## Direction contract

THESIS: The category standard played straight, at the craft of Linear and Hyperliquid: a sleek graphite console where the shell recedes and live state is the only thing that moves. It refuses the KPI tile grid, neon glow, and glass.

OWN-WORLD: Near black graphite in three steps (sidebar on the darkest, the content pane one step up, controls one more), one pixel seams instead of cards, 4 px corners, IBM Plex Sans at 13 to 14 px for words, Plex Mono tabular for every figure, one consistent 1.5 stroke icon set, color only for state.

STORY: You open it and see, before anything else, whether the trader's pulse is steady and what changed last; you trust every number because its age sits beside it; you can stop the bot from any page in one tap.

FIRST VIEWPORT: Desktop: a 232 px sidebar (mark, nav with icons, session and log out at the foot) beside a pane whose top bar carries env badge, page title, a status cluster (link, trader age, last command) and the stop alone at the right edge. Overview leads with the heartbeat tape across the full pane width, then Activity (wide) beside Position and P&L (narrow). Phone: top bar with badge, compact status, and stop; tabs at the bottom.

FORM: canon, the standing exit, chosen after three rolls; craft bar Linear and Hyperliquid; seed key 1eac03c9. Signature interaction: the heartbeat tape, one tick per second of real trader age, gaps where the link drops, and the link dot that pulses once on every event received. Motion grammar: 120 ms ease out state changes, one pulse per real event, nothing else moves.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved

The Activity feed has no data source yet (fills and alerts need later features); it ships as a designed region with its empty state.
