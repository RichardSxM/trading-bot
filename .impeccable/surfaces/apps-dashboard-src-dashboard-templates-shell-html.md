---
version: 1
slug: "apps-dashboard-src-dashboard-templates-shell-html"
primary_target: "apps/dashboard/src/dashboard/templates/shell.html"
related_targets: ["apps/dashboard/src/dashboard/templates/pages/overview.html","apps/dashboard/src/dashboard/templates/pages/portfolio.html","apps/dashboard/src/dashboard/templates/login.html","apps/dashboard/src/dashboard/templates/pages/styleguide.html"]
---

## Scope

The dashboard shell and every page inside it: top bar, side nav and phone tab bar, Overview, Portfolio, Trades, Strategies, System, Reconciliations, login, and /styleguide. Mode: Operate. Replaces the 2026-10-02 graphite console (seed 1eac03c9) at the user's request for a full redesign, 2026-11-12.

## Audience and job

One trader in the Netherlands supervising a 12 coin daily trend book (trend_vol, demo) on a laptop at a desk in daylight and a phone in a hand. First read: is the book up, which coins are long or short and how close each is to flipping, when the next rebalance runs, is the trader healthy, where is the stop.

## Direction contract

THESIS: The book as a league table: 12 coins are 12 clubs in one standings table ranked by 30 day trend, longs above and shorts below an oranje flip line. It refuses the KPI tile grid, the graphite exchange terminal, and per coin cards.

OWN-WORLD: Printed standings on a light paper ground (#f6f6f3) in near black ink; long green, short red, and oranje reserved only for the flip line and what changes at the next rebalance. Hairline row rules, zone stripes at the row's left edge, form guide squares, a condensed workhorse sans for words and tabular figures; no cards, no shadows, no glow. Raises: book value at poster scale (from Metro); oranje reserved for one meaning (from Orienteering); gross drawn against its 5x cap as a fixed bar (from J-card); ink and paper for everything else (from Datamatics); phone rows repack whole, no sideways scroll (from Convention catalog).

STORY: You open it and read book value and the season curve first, then scan the table: who is long, who is short, who sits near the line and could flip at 00:05 UTC; you trust each figure because its age sits beside it; you can stop the bot from any page in one tap.

FIRST VIEWPORT: Desktop: slim top bar (DEMO badge, nav, status, stop at the right edge). A scoreboard band: book value at poster size with today and since start, the equity curve as the season across the middle, next matchday (rebalance time UTC and Amsterdam, countdown), gross against cap, health. Below, filling the rest of the viewport: the 12 row standings table: rank, coin, side, 30 day trend, distance to flip, weight, unrealized P&L, 7 day form; the oranje flip line between the last long and first short. Activity moves below the fold, collapsed to what is unresolved. Phone: badge, status, stop; book value and curve; the table as whole rows.

FORM: The League Table (Eredivisie standings), candidate 7 of 7 on the ordered list, assigned by the roll; seed key 73e96693. Code-led. Signature interaction: the flip line is live; each coin's distance to it updates with every mark and a coin within one daily move of the line gets an oranje near-flip tick. Motion grammar: 120 ms ease out state changes; a changed figure flashes its row once; nothing else moves.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Data decisions

Distance to flip: the dashboard computes it from Kraken public daily closes (user's choice 2026-11-12, no trader change). Equity curve: account_snapshots of the main trader's runs.

## Unresolved

None yet.
