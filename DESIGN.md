---
name: Kraken Futures Scalper
description: The book as a league table. Twelve coins ranked by 30 day trend on printed standings, longs above and shorts below one oranje flip line, book value set at poster size, and the stop one tap away.
colors:
  bg: "#F3F4F1"
  surface: "#FCFCFB"
  raised: "#E8E9E5"
  rule: "#D8DAD4"
  edge: "#7C8079"
  text: "#111315"
  muted: "#464B52"
  faint: "#5C6168"
  profit: "#0A7445"
  loss: "#C0102B"
  warn: "#8F5300"
  info: "#2648C9"
  live: "#B0102A"
  demo: "#8F5300"
  stop: "#C0102B"
  flip: "#E04F00"
  flip-ink: "#A33A00"
  on-alarm: "#FFFFFF"
typography:
  poster:
    fontFamily: '"Barlow Semi Condensed", "Barlow", system-ui, sans-serif'
    fontSize: 64px
    fontWeight: 600
    lineHeight: 0.95
    letterSpacing: -0.02em
    fontFeature: '"tnum"'
  display:
    fontFamily: '"Barlow Semi Condensed", "Barlow", system-ui, sans-serif'
    fontSize: 32px
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: -0.01em
  headline:
    fontFamily: '"Barlow Semi Condensed", "Barlow", system-ui, sans-serif'
    fontSize: 24px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.01em
  title:
    fontFamily: '"Barlow Semi Condensed", "Barlow", system-ui, sans-serif'
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.25
  body-lg:
    fontFamily: '"Barlow", system-ui, sans-serif'
    fontSize: 17px
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontFamily: '"Barlow", system-ui, sans-serif'
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: '"Barlow", system-ui, sans-serif'
    fontSize: 14px
    fontWeight: 500
    lineHeight: 1.35
  caption:
    fontFamily: '"Barlow Semi Condensed", "Barlow", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: 0.08em
  number:
    fontFamily: '"Barlow", system-ui, sans-serif'
    fontSize: 15px
    fontWeight: 500
    lineHeight: 1.45
    fontFeature: '"tnum"'
  number-lg:
    fontFamily: '"Barlow Semi Condensed", "Barlow", system-ui, sans-serif'
    fontSize: 24px
    fontWeight: 600
    lineHeight: 1.15
    fontFeature: '"tnum"'
rounded:
  "2": 2px
  "4": 4px
spacing:
  "4": 4px
  "8": 8px
  "12": 12px
  "16": 16px
  "24": 24px
  "32": 32px
  "48": 48px
motion:
  state: 120ms ease-out
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.2}"
    height: 44px
  button-primary:
    backgroundColor: "{colors.text}"
    textColor: "{colors.bg}"
    typography: "{typography.label}"
    rounded: "{rounded.2}"
    height: 44px
  button-quiet:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.2}"
    height: 44px
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.loss}"
    rounded: "{rounded.2}"
    height: 44px
  button-stop:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-alarm}"
    typography: "{typography.label}"
    rounded: "{rounded.2}"
    height: 44px
  badge-demo:
    backgroundColor: "{colors.demo}"
    textColor: "{colors.bg}"
    typography: "{typography.caption}"
    rounded: "{rounded.2}"
    height: 20px
  badge-live:
    backgroundColor: "{colors.live}"
    textColor: "{colors.on-alarm}"
    typography: "{typography.caption}"
    rounded: "{rounded.2}"
    height: 20px
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.2}"
    height: 44px
  input-code:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.number-lg}"
    rounded: "{rounded.2}"
    height: 44px
  masthead:
    backgroundColor: "{colors.text}"
    textColor: "{colors.bg}"
    typography: "{typography.label}"
    padding: "0 {spacing.24}"
    height: 56px
  nav-item:
    textColor: "{colors.bg}"
    typography: "{typography.label}"
    padding: "0 {spacing.12}"
    height: 56px
  tab-bar:
    backgroundColor: "{colors.text}"
    textColor: "{colors.bg}"
    typography: "{typography.caption}"
    height: 60px
  scoreboard:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.poster}"
    padding: "{spacing.24}"
  standings-row:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.number}"
    height: 48px
  flip-line:
    backgroundColor: "{colors.flip}"
    textColor: "{colors.flip-ink}"
    typography: "{typography.caption}"
    height: 2px
  region:
    backgroundColor: "{colors.surface}"
    padding: "{spacing.24}"
  table-cell:
    textColor: "{colors.text}"
    typography: "{typography.label}"
    padding: "{spacing.8} {spacing.16}"
  table-head:
    textColor: "{colors.faint}"
    typography: "{typography.caption}"
    padding: "{spacing.8} {spacing.16}"
# Extension (spec 0004 AC-15): every pair the parity test checks for WCAG AA.
# use: text = 4.5:1, large = 3:1 (18.66px bold or 24px regular and up), ui = 3:1 (a control's only boundary, an icon, a dot).
contrast_pairs:
  - {fg: text, bg: bg, use: text}
  - {fg: text, bg: surface, use: text}
  - {fg: text, bg: raised, use: text}
  - {fg: muted, bg: bg, use: text}
  - {fg: muted, bg: surface, use: text}
  - {fg: muted, bg: raised, use: text}
  - {fg: profit, bg: bg, use: text}
  - {fg: profit, bg: surface, use: text}
  - {fg: profit, bg: raised, use: text}
  - {fg: loss, bg: bg, use: text}
  - {fg: loss, bg: surface, use: text}
  - {fg: loss, bg: raised, use: text}
  - {fg: warn, bg: bg, use: text}
  - {fg: warn, bg: surface, use: text}
  - {fg: warn, bg: raised, use: text}
  - {fg: info, bg: bg, use: text}
  - {fg: info, bg: surface, use: text}
  - {fg: info, bg: raised, use: text}
  - {fg: faint, bg: bg, use: text}
  - {fg: faint, bg: surface, use: text}
  - {fg: faint, bg: raised, use: text}
  - {fg: flip-ink, bg: bg, use: text}
  - {fg: flip-ink, bg: surface, use: text}
  - {fg: flip-ink, bg: raised, use: text}
  - {fg: on-alarm, bg: stop, use: text}
  - {fg: on-alarm, bg: live, use: text}
  - {fg: bg, bg: demo, use: text}
  - {fg: bg, bg: info, use: text}
  - {fg: bg, bg: text, use: text}
  - {fg: edge, bg: bg, use: ui}
  - {fg: edge, bg: surface, use: ui}
  - {fg: edge, bg: raised, use: ui}
  - {fg: flip, bg: bg, use: ui}
  - {fg: flip, bg: surface, use: ui}
---

# Design System: Kraken Futures Scalper

## Overview

**Creative North Star: "The League Table"**

The dashboard reads like the Monday standings page: one trader's book of coins printed as a table on paper, ranked by their 30 day trend, with the oranje flip line drawn where the zones split. Coins above the line plan long at the next rebalance and coins below it plan short, so the page answers its first question (who holds what, and who could flip tonight) by position alone. Book value is set at poster size, because it is the one figure you read from across the room.

The world is ink on paper. Sections open under a two pixel ink rule with a condensed uppercase title, rows part on hairlines, and figures sit in tabular columns. Colour is reserved: green and red say long and short, oranje says the flip line and whatever changes at the next rebalance, and everything else, P&L included, is ink with a sign and an arrow. The shell is a black masthead with the page names in condensed capitals; the status line and the stop sit on paper under it. Nothing is a card, nothing glows, nothing floats.

Rejected on purpose: the graphite exchange terminal this replaced (2026-11-12), the KPI tile grid, per coin cards on the first screen, neon on black, and glass.

**Key Characteristics:**
- Paper ground, ink type, hairline rows, two pixel ink rules over every section.
- Book value at poster size; the standings table carries the rest of the first screen.
- Green and red only for long and short; oranje only for the flip line and what changes at the next rebalance.
- One condensed signage family for words and tabular figures; no mono costume.
- One authored motion: a coin whose rank, side, or flip state changed flashes its row once.

## Colors

A newsprint palette: one cool paper, one near black ink, two side colours, and one oranje that means a single thing.

### Primary
- **Ink** (`text`): every word and figure, section rules (2 px), the masthead fill, the primary button, filled form squares, the season curve above its opening value.

### Secondary
- **Long Green** (`profit`): the side long, wherever it appears: zone stripes, the word `Long`, a rising 30 day trend, the long count. Also the profit tint of the `pnl` component outside the Overview's table and scoreboard.
- **Short Red** (`loss`): the side short and a falling trend, the same places; also errors and the loss tint of `pnl` outside the Overview.

### Tertiary
- **Oranje** (`flip`): the flip line across the standings, the small tick before `near the line` and `flips at`, the flip line in the brand mark, and the one time row flash. Never a fill for a control.
- **Oranje Ink** (`flip-ink`): text in the flip voice: the flip line label, `near the line`, `flips at 00:05 UTC`, and the scoreboard's flip count.

### Neutral
- **Newsprint** (`bg`): the page ground and the masthead's type.
- **Sheet** (`surface`): inputs, the season chart's well, and a hovered row.
- **Pressed** (`raised`): button hover, the gross gauge's track, loading bars.
- **Hairline** (`rule`): rules between rows and in the status strip.
- **Ruling** (`edge`): input borders, dotted time underlines, a flat coin's empty stripe.
- **Pencil** (`muted`): secondary text, held sizes, the season curve below its opening value.
- **Margin** (`faint`): captions, column heads, ranks, daily move counts.
- **Amber Watch** (`warn`), **Link Blue** (`info`): warnings and links, as before.
- **Demo Amber** (`demo`), **Live Red** (`live`), **Stop Red** (`stop`), **White** (`on-alarm`): the env badges and the stop.

### Named Rules
**The Two Sides Rule.** Green means long and red means short. A figure that is neither a side nor a trend (P&L, the season curve) on the Overview reads in ink, by its sign and arrow.

**The One Oranje Rule.** Oranje means the flip line and what changes at the next rebalance, nothing else. If a new element wants oranje, it must be about a flip.

## Typography

**Display Font:** Barlow Semi Condensed (with Barlow, system-ui)
**Body Font:** Barlow (with system-ui)

**Character:** A DIN-like road signage family: condensed capitals for titles, labels, and the poster figure, the regular width for reading, tabular figures (`tnum`) for every number. One family, two widths, no monospace.

### Hierarchy
- **Poster** (600, 64 px, clamped to 48 px on a phone, 0.95): book value only.
- **Display** (600, 32 px, 1.1): the login title.
- **Headline** (600, 24 px, 1.2, uppercase): the page title in the status line.
- **Title** (600 to 700, 20 px, 1.25, uppercase with 0.02em tracking): section titles; coin names in the standings at 700, not uppercased.
- **Body** (400, 15 px, 1.45) and **Body large** (17 px, inputs).
- **Label** (500, 14 px): table cells, meta lines, controls.
- **Caption** (600, 12 px, 0.08em, uppercase, Semi Condensed): column heads, figure labels, the flip line label.
- **Number** (500, 15 px, `tnum`) and **Number large** (600, 24 px, Semi Condensed, `tnum`): table figures and the scoreboard's facts.

### Named Rules
**The Signage Rule.** Labels and titles are condensed capitals; reading text and figures are not. Never set a sentence in capitals.

## Layout

The page is a single column on paper under the masthead, 24 px gutters on a laptop and 16 px on a phone, capped at 1,680 px. The Overview stacks the scoreboard, the standings, Activity, and the per coin ledger, each under its ink rule with 32 px between sections.

The scoreboard is three columns from 1,200 px (book value, the season curve, the facts), two from 900 px (book and facts, the season under them), and one on a phone. The standings are a full width table; under 760 px each coin becomes a whole row of two lines (rank, stripe, coin, side and size, P&L; then trend, distance to flip, form), never a sideways scroll. Rows are 52 px tall on a laptop. Activity scrolls inside a 26 rem cap; the ledger opens on demand.

### Named Rules
**The Whole Row Rule.** On a phone a table row repacks whole; nothing scrolls sideways.

## Elevation & Depth

Flat. Depth is only order and rules: a two pixel ink rule opens a section, a hairline parts rows, and the masthead is a solid ink band. There are no shadows, glows, gradients, or blurs anywhere; the season chart's soft fill under its line is data, not depth.

### Named Rules
**The Printed Page Rule.** If it could not be printed in two inks on newsprint, it does not belong.

## Shapes

Square. Controls and badges take a 2 px corner; tables, sections, gauges, form squares, and stripes are sharp. The zone stripe is a 6 by 28 px bar at the start of each coin row; form squares are 20 px (14 px on a phone).

## Components

Each component is a Jinja macro in `apps/dashboard/src/dashboard/templates/components/` unless named otherwise; `/styleguide` renders every state listed here.

### Number (`num`)
Plain, right aligned in tables, Barlow with tabular figures and a true minus sign.
- **States:** `positive`, `negative`, `zero`, `grouped`

### P&L (`pnl`)
A sign, an arrow, and the figure at 600. Profit and loss tints apply outside the Overview's standings and scoreboard; there it reads in ink (The Two Sides Rule).
- **States:** `profit`, `loss`, `zero`

### Timestamp (`ts`)
UTC text with a dotted underline and the Amsterdam time on hover or long press.
- **States:** `utc`

### Badge (`badge`)
Condensed Caption capitals at 700, 20 px tall, 2 px corners. The env badge is solid (Demo Amber or Live Red); status badges are a 1 px outline in their state colour.
- **States:** `demo`, `live`, `ok`, `warn`, `bad`, `neutral`

### Status dot (`status_dot`)
An 8 px circle beside a text label; never the only signal.
- **States:** `ok`, `warn`, `bad`, `off`

### Stat (`stat`)
A Caption label over a Number large value; stale dims it and turns the note Amber Watch, error replaces it with a Short Red label.
- **States:** `default`, `stale`, `error`, `loading`

### Table (`table`)
Label size rows on hairlines under a sticky head of Caption capitals and a 2 px ink rule; numbers right aligned; a hovered row turns Sheet.
- **States:** `default`, `empty`, `loading`

### Pager (`pager`)
Under a paged table: the page as a Caption between `Newer` and `Older` links in ink at 600, each at least 44 px tall.
- **States:** `first`, `middle`, `past_end`

### Button (`button`)
- **Shape:** 2 px corners, at least 44 × 44 px, 16 px side padding, Label at 600.
- **Default:** Sheet fill, ink border and text; hover Pressed. **Focus:** 2 px ink outline, 2 px offset.
- **Primary:** ink fill, Newsprint text; hover Pencil.
- **Quiet:** no fill or border, Pencil text.
- **Danger:** Short Red border and text.
- **Stop:** Stop Red fill, white condensed capitals `STOP` with an 18 px octagon; hover Live Red, pressed ink. Always visible, never disabled by the client.
- **Disabled / Busy:** 45 percent opacity; busy sweeps a 2 px rule under the label and ignores clicks.
- **States:** `default`, `quiet`, `danger`, `stop`, `disabled`, `busy`

### Empty state (`empty_state`)
A bare icon in Margin beside one Pencil sentence saying what will appear and when.
- **States:** `default`

### Loading (`loading`)
Three square skeleton bars in Pressed that fade gently.
- **States:** `default`

### Error state (`error_state`)
A Short Red octagon and title naming the problem, then a Pencil sentence with the recovery.
- **States:** `default`

### Stale wrapper (`stale`)
A live value with its age in Caption; Amber Watch past the amber limit, Short Red past red, dimmed with `link lost`.
- **States:** `ok`, `warn`, `bad`, `lost`

### Chart (`chart`)
A Lightweight Charts canvas themed from the tokens with a `Charting by TradingView` text link (the logo needs an inline style the CSP forbids). On the Overview it draws the season curve (below).
- **States:** `default`

### Nav item (`nav_links`)
Condensed capitals in the masthead, Newsprint at 72 percent on ink, full Newsprint with a 3 px Newsprint underline when current. On a phone the links become the bottom tab bar, icon over label, the current tab marked by a bar along its top.
- **States:** `default`, `current`

### Link status (`link`)
The first item of the status line: an 8 px dot, a Margin `link` label, and a word; the dot beats once on every event received.
- **States:** `static`, `connecting`, `live`, `degraded`, `reconnecting`, `lost`

### Command state (`command`)
The latest command and its outcome with its age, in the outcome's colour.
- **States:** `none`, `pending`, `ok`, `warn`, `bad`, `lost`

### Alert bar (`alert`)
The one loud band: under the status line, a lightly tinted Sheet with a 1 px Short Red (or Amber Watch) top rule, an icon, a bold title, and what to do; empty while all is well.
- **States:** `stop_unhandled`, `stop_failed`, `stop_late`, `stop_unknown`, `trader_silent`, `stop_unacknowledged`, `no_data`

### Icon (`icon`)
Lucide, inline, one 1.5 stroke, 16 px by default.
- **States:** `default`

### Pulse (`pulse`)
The heartbeat tape, kept on the styleguide; the Overview no longer shows it (the trader's age sits in the status line).
- **States:** `default`

### Activity feed (`activity`)
Commands and alerts, newest first, on hairlines; repeats collapse into one row with a count; it scrolls inside its cap.
- **States:** `empty`, `filled`, `collapsed`, `error`

### Position card (`position`)
The per coin ledger at the foot of the Overview, opened on demand: per coin blocks of label and value rows on hairlines, tiled four across on a laptop.
- **States:** `open`, `flat`, `offline`, `bad`, `lost`, `pnl_unavailable`

### Wanted, Trader, Kraken (`sides`)
Three label over value columns at the top of each coin's ledger block; a disagreement turns both values Short Red at 700.
- **States:** `agree`, `disagree`, `unknown`

### Kraken check (`check`)
A cue in the status line: `kraken check clean · 12s`, Amber Watch while checking, Short Red on a mismatch, a halt, or an unknown view; links to the Reconciliation page.
- **States:** `clean`, `checking`, `mismatch`, `halted`, `unknown`, `lost`

### Entries blocked (`blocked`)
An Amber Watch cue in the status line, and above the standings a title over one row per reason.
- **States:** `none`, `one`, `several`

### Journal check (`journal`)
The Trades page's export check: a verdict badge, both nets, the totals table, and the downloads; a mismatch sits on a lightly tinted Amber Watch field.
- **States:** `match`, `mismatch`, `not_comparable`, `unlinked`, `refused`, `error`

### Scoreboard
The Overview's opening band (`partials/overview_score.html`). Book value at Poster size with `USD` in Caption; today since 00:00 UTC and the season (strategy and start date) as ink P&L. The facts in a two by two grid on hairlines: next rebalance as a countdown over its UTC and Amsterdam time, gross over its cap with a fixed length gauge (ink fill on Pressed, never overflowing), the book as `n long · n short` in the side colours with any flip count in Oranje Ink, and the trader state badge.

### Season curve
`static/js/season.js`: the book's value per hour over every run of the current strategy, a baseline series around the season's first value, ink above and Pencil below with faint fills, no grid verticals, no scroll or zoom, redrawn every five minutes.

### Standings row
`partials/overview_standings.html`, the signature. Rank in Margin; the coin name at Title 700 after its zone stripe (Long Green, Short Red, or an empty Ruling outline when flat); the side and size; the live 30 day trend in the side colour; the flip price; the move to it with its size in daily moves, or `near the line` / `flips at 00:05 UTC` in Oranje Ink with an oranje tick; weight; ink P&L; and seven form squares (won filled ink, lost outlined, unknown dotted). The flip line is a full width oranje rule with its label in Oranje Ink, between the last coin with a positive trend and the first without; an empty zone says so in a Pencil row. A coin whose rank, side, or flip state changed flashes its row once in a faint oranje; price ticks alone never flash.

## Do's and Don'ts

### Do:
- **Do** run every number through `num`, `pnl`, or `ts`, and pass `Decimal` values only.
- **Do** open every section with a 2 px ink rule and a condensed uppercase title.
- **Do** keep green and red for long and short, and oranje for the flip line and what changes at the next rebalance.
- **Do** keep the stop at the right edge of the status line on every page, at least 44 × 44 px.
- **Do** add a new colour combination to `contrast_pairs` before using it.
- **Do** keep motion to 120 ms state changes, the link beat, and the one row flash; nothing moves under `prefers-reduced-motion`.

### Don't:
- **Don't** use shadows, glows, gradients, blurs, glass, or cards.
- **Don't** put coins in tiles or cards on the first screen; they are rows in the standings.
- **Don't** set figures in a monospace face; use Barlow's tabular figures.
- **Don't** use a `float` for money, or show a local time outside a tooltip or beside its UTC time.
- **Don't** write inline `style` or `script`, or `hx-on` attributes; the CSP blocks them.
- **Don't** load anything from another origin: fonts, icons, and libraries are self hosted.
