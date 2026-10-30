---
name: Kraken Futures Scalper
description: A graphite console for one trader supervising an automated scalper, where the shell recedes, live state is the only thing that moves, and the stop is always one tap away.
colors:
  bg: "#08090B"
  surface: "#0E1013"
  raised: "#15181D"
  rule: "#1D2128"
  edge: "#5F6876"
  text: "#EDEEF0"
  muted: "#9AA2AE"
  faint: "#7E8694"
  profit: "#2EBD85"
  loss: "#F0524F"
  warn: "#E8A33D"
  info: "#7B86F2"
  live: "#D62F2F"
  demo: "#E8A33D"
  stop: "#C93338"
  on-alarm: "#FFFFFF"
typography:
  display:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 32px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: -0.02em
  headline:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 24px
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: -0.01em
  title:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.3
  body-lg:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.4
  caption:
    fontFamily: '"IBM Plex Sans", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: 0.06em
  number:
    fontFamily: '"IBM Plex Mono", ui-monospace, monospace'
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.45
    fontFeature: '"tnum"'
  number-lg:
    fontFamily: '"IBM Plex Mono", ui-monospace, monospace'
    fontSize: 20px
    fontWeight: 500
    lineHeight: 1.3
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
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.4}"
    height: 44px
  button-primary:
    backgroundColor: "{colors.text}"
    textColor: "{colors.bg}"
    typography: "{typography.label}"
    rounded: "{rounded.4}"
    height: 44px
  button-quiet:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.4}"
    height: 44px
  button-danger:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.loss}"
    rounded: "{rounded.4}"
    height: 44px
  button-stop:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-alarm}"
    typography: "{typography.label}"
    rounded: "{rounded.4}"
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
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    typography: "{typography.body-lg}"
    rounded: "{rounded.4}"
    height: 44px
  input-code:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    typography: "{typography.number}"
    rounded: "{rounded.4}"
    height: 44px
  sidebar:
    backgroundColor: "{colors.bg}"
    padding: "{spacing.16} {spacing.12}"
    width: 232px
  nav-item:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.4}"
    padding: "0 {spacing.8}"
    height: 36px
  nav-item-current:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.text}"
    rounded: "{rounded.4}"
    height: 36px
  tab-bar:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.muted}"
    typography: "{typography.caption}"
    height: 60px
  pane:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.4}"
  topbar:
    backgroundColor: "{colors.surface}"
    typography: "{typography.label}"
    padding: "{spacing.8} {spacing.8} {spacing.8} {spacing.24}"
    height: 60px
  region:
    backgroundColor: "{colors.surface}"
    padding: "{spacing.24}"
  table-cell:
    textColor: "{colors.text}"
    typography: "{typography.label}"
    padding: "{spacing.8} {spacing.24}"
  table-head:
    textColor: "{colors.faint}"
    typography: "{typography.caption}"
    padding: "{spacing.8} {spacing.24}"
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
  - {fg: on-alarm, bg: stop, use: text}
  - {fg: on-alarm, bg: live, use: text}
  - {fg: bg, bg: demo, use: text}
  - {fg: bg, bg: info, use: text}
  - {fg: edge, bg: bg, use: ui}
  - {fg: edge, bg: surface, use: ui}
  - {fg: edge, bg: raised, use: ui}
  - {fg: bg, bg: text, use: text}
---

# Design System: Kraken Futures Scalper

## Overview

**Creative North Star: "The Graphite Console"**

The category standard for a trading console, played straight and finished to the craft of Linear and Hyperliquid. The shell recedes: a sidebar on the darkest graphite, a content pane one step lighter set into it, controls one step lighter again. The only things that move are the things that are live. This is an instrument panel for one person supervising a bot, read on a laptop at a desk and on a phone in a hand, and at both sizes the same facts come first: what just happened, whether the picture is fresh, whether this is DEMO or LIVE, and where the stop is.

Calm is the default and the point. Neutral graphite carries structure; color is reserved for state (profit, loss, stale, live, stop), so when color appears it is information. Regions are cut apart by 1 px seams, never boxed as cards. Density is welcome: figures sit in tight, aligned columns because the trader reads them as columns. The world refuses the KPI tile grid, neon glow, and glass.

The signature is the heartbeat: a tape of the trader's data age, one tick a second, and a link dot that beats once for every event the dashboard receives. If the tape is steady and the dot is beating, the picture is live.

**Key Characteristics:**
- Dark only, three graphite steps: sidebar on Night, pane on Desk, controls on Lamp.
- Regions meet at 1 px seams over the Hairline color; no cards, no shadows.
- IBM Plex Sans at 13 to 14 px for words, IBM Plex Mono with tabular figures for every figure.
- One icon set (Lucide) at one stroke (1.5).
- Color only for state; the stop is crimson and always at the top right of the pane.
- Every live value carries its age or a `link lost` or `no data` marker.
- Calm until something is wrong, then unmissable: one alert bar under the top bar says what broke and what to do.

## Colors

A near black, slightly cool graphite ramp with a small, strict set of signal colors.

### Primary
- **Signal Indigo** (info): focus rings, text links, text selection, the `connecting` link state, and a command that is in flight. It is the only interaction accent and it never fills a button.

### Secondary
- **Ledger Green** (profit): positive P&L, the `live` link dot, healthy dots and outline badges. Never for buttons or decoration.
- **Ledger Red** (loss): negative P&L, failures, `lost`, `Stop NOT sent`, the error state.
- **Amber Watch** (warn): stale ages above the amber threshold, `reconnecting`, the `link lost` marker, the `no data` link state, warning alerts, the session ending cue.

### Tertiary
- **Live Red** (live): the LIVE badge, the 2 px top border of the top bar in live, and the pressed stop. Means real money.
- **Demo Amber** (demo): the DEMO badge. The same hue as warn on purpose: demo is a caution, not a state of calm.
- **Stop Crimson** (stop): the stop button only, with On alarm text.

### Neutral
- **Night** (bg): the page behind everything, the sidebar, the phone tab bar, inputs.
- **Desk** (surface): the content pane, the top bar, every region, table heads.
- **Lamp** (raised): default buttons, the current nav item, hovered rows, loading bars, notices.
- **Hairline** (rule): the seams between regions, row rules, status strip dividers, the pane border. Never the only edge of a control.
- **Edge** (edge): input borders, button borders on hover, off dots, the dashed gap line in the tape; 3:1 or better on every surface.
- **Paper** (text): primary text and figures, and the fill of the primary button.
- **Pencil** (muted): secondary text, empty state sentences, idle nav items, fresh ticks in the tape.
- **Margin** (faint): labels only (`link`, `trader`, `command`, stat labels, table heads, region meta) and empty state icons, never values.
- **On alarm** (on-alarm): white text on stop and live.

### Named Rules
**The Meaning Rule.** Every color other than the neutrals means a state. If an element is colored and you cannot name the state it reports, it should be gray.

**The Pair Rule.** Every text and background combination in use is listed in `contrast_pairs` and passes WCAG AA; a new combination is added there first, and the token test enforces it.

**The Paper Primary Rule.** The one primary action on a screen is Paper with Night text. Indigo marks attention (focus, links, work in flight), never a filled action.

## Typography

**Body Font:** IBM Plex Sans (with `system-ui`, `sans-serif`)
**Number Font:** IBM Plex Mono (with `ui-monospace`, `monospace`), always with tabular figures

**Character:** Plex is an engineer's typeface: plain, sturdy, slightly mechanical. The console runs small, at 13 to 14 px, the way Linear does; hierarchy comes from weight (600 against 400 and 500) and from Paper against Margin, not from size jumps.

### Hierarchy
- **Display** (600, 32 px, 1.2): reserved; not used in the built screens.
- **Headline** (600, 24 px, 1.25): the login heading only.
- **Title** (600, 20 px, 1.3): reserved for page level headings; its size also sets the authenticator code input.
- **Body large** (400, 16 px, 1.5): login inputs, so a phone never zooms.
- **Body** (400, 14 px, 1.45): default text. The page title in the top bar is Body at 600.
- **Label** (500, 13 px, 1.4): the status strip, nav items, buttons, form labels, table cells. Region titles are Label at 600.
- **Caption** (500, 12 px): table heads, stat labels, region meta, ages, the tape legend. Badges are Caption at 600, uppercase, with 0.06em tracking.
- **Number** (Mono 400, 14 px, tabular): every figure in a line or a table.
- **Number large** (Mono 500, 20 px, tabular): the value in a stat.

### Numbers and time
These rules are implemented by the `num`, `pnl`, and `ts` template filters; templates never format numbers by hand.
- **Decimal only.** `num`, `pnl` refuse a `float`, `int`, or `bool` (TypeError) and NaN or infinity (ValueError). Callers quantize to the instrument's precision; the filters never round.
- **Own precision.** A value shows exactly the digits its `Decimal` carries: `1234.50` → `1,234.50`, `1E+3` → `1,000`.
- **Grouping and sign.** Comma thousands, dot decimal. Negatives use the true minus sign `−` (U+2212), which matches the plus sign in width. Negative zero shows as `0`.
- **P&L.** `pnl` adds `+` or `−`, ▲ or ▼, and the profit or loss color. Zero is neutral, with no sign and no arrow. The arrow is hidden from screen readers; the sign carries the meaning.
- **Time.** Every timestamp is UTC, truncated to seconds, written `2026-10-02 14:03:07Z`. Amsterdam local time appears only in the `title` tooltip (`16:03:07 CEST, Amsterdam`), marked by a dotted Edge underline.
- **Right aligned.** Numbers in tables align right so digits of the same place line up.

### Named Rules
**The Mono for Measurement Rule.** Mono is for numbers, ids, codes, and the tape's time axis. A word set in mono is a costume.

**The Sentence Case Rule.** Titles, labels, and table heads are sentence case. Only badges are uppercase.

## Layout

**Laptop (900 px and wider).** A 232 px sidebar on Night holds the mark, the four nav items, and at its foot the session cue and Log out; it is sticky at full height. Beside it the content pane sits inset by 8 px on the top, right, and bottom, with a 1 px Hairline border and 4 px corners, so the sidebar's darker tone frames it. The pane runs edge to edge: there is no centered maximum width.

**The top bar** lives inside the pane, sticky, at least 60 px tall, on Desk with a Hairline rule below. From left: the env badge and the page title, then the status strip (link, trader age, last command) whose items are divided by 1 px Hairline rules with 12 px either side, then the stop alone at the right edge behind its own 1 px rule. In live the bar gains a 2 px Live Red top border. While the link is reconnecting or lost, the age and command dim to half and an amber `link lost` marker joins the strip. While something is wrong, the alert bar runs full width along the bar's bottom edge, inside the sticky bar, so it never scrolls away.

**Regions.** A page is a board: a grid with 1 px gaps laid over the Hairline color, so every region (on Desk, 24 px padding) is separated by a seam rather than a box. From 1100 px the board has two columns, a wide one and a side column of 320 to 400 px; wide regions span both. Overview leads with the heartbeat tape across the full width, then Activity beside Position and P&L. A region head is a Label 600 title with a Caption meta line beside it, then 16 px of space.

**Phone (below 900 px).** The sidebar becomes a four tab bar fixed to the bottom on Night with a Hairline rule on top: 60 px tabs, a 20 px icon over a Caption label, safe area aware. The top bar keeps the badge, title, and stop on its first row; the status strip wraps onto a second row under them, so the stop never leaves the first viewport. Regions stack with 16 px padding. A session footer under the board, ruled on top, carries the session cue and a quiet Log out.

**Spacing** follows a 4 px base (4, 8, 12, 16, 24, 32, 48): 4 inside a label pair, 8 to 12 inside groups, 16 between related groups, 24 for region padding and top bar gaps on a laptop. Tables scroll inside their own container and bleed to the region edges; the page never scrolls sideways.

### Named Rules
**The Seam Rule.** Regions are separated by 1 px seams (a grid gap over Hairline, or a 1 px rule), never by cards, outlines around each region, or extra background tones.

## Elevation & Depth

No shadows anywhere. Depth is tonal and runs in three steps: Night (sidebar, tab bar, page) under Desk (the pane, the top bar, regions) under Lamp (controls, the current nav item, hovered rows). Hairline rules mark where two areas meet. A sticky element (the top bar, a table head) keeps its own Desk tone and a rule along its edge, which is enough to separate it from content scrolling beneath.

### Named Rules
**The Flat Desk Rule.** If something needs to stand out, give it a different surface tone or a rule, never a shadow, a glow, or a blur.

## Shapes

Corners are nearly square: 2 px for badges, notices, and focus outlines; 4 px for buttons, inputs, nav items, the pane, and the brand tile. Nothing is pill shaped; status and link dots are 8 px circles. Borders are 1 px; the only thicker lines are the 2 px Live Red top border in live, the 2 px state colored top rule of the alert bar, and the 2 px focus outline. Icons are Lucide, drawn on a 24 px grid with a 1.5 stroke and round caps and joins, at 16 px (20 px in the tab bar, 18 px in the stop). The brand mark is a Lamp tile with an Edge outline and a Paper heartbeat trace.

## Components

Each component is a Jinja macro in `apps/dashboard/src/dashboard/templates/components/`; `/styleguide` renders every state listed here.

### Number (`num`)
Plain, right aligned in tables, Mono with tabular figures.
- **States:** `positive`, `negative`, `zero`, `grouped`

### P&L (`pnl`)
The only component that colors a number by its sign.
- **States:** `profit`, `loss`, `zero`

### Timestamp (`ts`)
UTC text with the local time on hover or long press.
- **States:** `utc`

### Badge (`badge`)
Uppercase Caption at 600 with 0.06em tracking, 20 px tall, 8 px side padding, 2 px corners. The env badge is a solid fill (Demo Amber with Night text, Live Red with On alarm text) and is the loudest object in the top bar on purpose. Status badges are quiet: a 1 px outline and text in their state color, no fill.
- **States:** `demo`, `live`, `ok`, `warn`, `bad`, `neutral`

### Status dot (`status_dot`)
An 8 px circle beside a text label; never the only signal. Off is an Edge ring with no fill.
- **States:** `ok`, `warn`, `bad`, `off`

### Stat (`stat`)
A Margin Caption label over a Number large value, with an optional Caption note under it; no box. Stale dims the value to 75 percent and turns the note amber; error replaces the value with a Ledger Red Label at 600; loading shows skeleton bars.
- **States:** `default`, `stale`, `error`, `loading`

### Table (`table`)
Dense rows at Label size, cells 8 px tall padding and 24 px side padding on a laptop (16 px on a phone), Hairline rules between rows, numbers right aligned in Mono. Heads are sentence case Caption in Margin with no fill of their own (they sit on Desk) and a Hairline rule below; they stick while the body scrolls. A hovered row turns Lamp. Empty and loading rows span every column, left aligned, in Pencil.
- **States:** `default`, `empty`, `loading`

### Pager (`pager`)
Under a paged table (Trades, spec 0006 AC-19), above a Hairline: the page as a Margin Caption (`Page 2`) between quiet links, `Newer` with a chevron left and `Older` with a chevron right, in Pencil at Label size that turn Paper on a Lamp fill when hovered, each at least 44 px tall. `Older` shows only while the page is full. A page past the end shows one Pencil line (`Nothing on page 9.`) and a `Back to page 1` link. A lone first page renders no pager.
- **States:** `first`, `middle`, `past_end`

### Button (`button`)
- **Shape:** 4 px corners, at least 44 × 44 px, 16 px side padding, Label type, an optional 16 px icon 8 px from the label.
- **Default:** Lamp fill, Edge border, Paper text; hover turns the border Pencil; pressed drops to Desk. **Focus:** 2 px Signal Indigo outline, 2 px offset.
- **Primary:** Paper fill, Night text, 600 weight; hover goes to pure white. One per screen (Log in).
- **Quiet:** no fill and no border, Pencil text; hover gives a Lamp fill and Paper text. For secondary actions such as Log out.
- **Danger:** Ledger Red text and border, for destructive actions that are not the stop.
- **Stop:** Stop Crimson fill, On alarm 600 text, an 18 px octagon icon, an On alarm border on hover, Live Red when pressed. Always visible, never disabled by the client.
- **Disabled:** 45 percent opacity, no hover, `not-allowed` cursor.
- **Busy:** the label stays, a 2 px rule in the text color sweeps under it, and the button ignores clicks. The stop takes this state while its request is in flight.
- **States:** `default`, `quiet`, `danger`, `stop`, `disabled`, `busy`

### Empty state (`empty_state`)
A bare Lucide icon in Margin (16 px, on a 20 px line, no tile) beside one sentence in Pencil saying what will appear here and when. No frame, no dashed box.
- **States:** `default`

### Loading (`loading`)
Three 10 px skeleton bars in Lamp at full, 80, and 55 percent width that fade gently (static under reduced motion); never a spinner in the middle of content.
- **States:** `default`

### Error state (`error_state`)
A Ledger Red octagon icon beside a Ledger Red 600 title that names the problem and a Pencil sentence with the recovery. No banner and no fill.
- **States:** `default`

### Stale wrapper (`stale`)
Wraps any live value with its age in Caption. Fresh shows the age in Margin; above the amber threshold it turns Amber Watch; above red it turns Ledger Red; with the link lost the whole value dims to 60 percent and reads `link lost`, so a frozen number never looks live.
- **States:** `ok`, `warn`, `bad`, `lost`

### Chart (`chart`)
A Lightweight Charts canvas, 240 px tall, themed from the tokens: a Night well inside its Desk region, Hairline grid and scale borders, Pencil axis text in Plex Mono, Ledger Green and Red for up and down. A Caption credit sits under each chart with a `Charting by TradingView` text link (the library's logo needs an inline style the CSP forbids).
- **States:** `default`

### Nav item (`nav_links`)
A 16 px icon 12 px from a Label, 36 px tall, 4 px corners, in Pencil. Hover lays Desk under it in Paper; the current page sits on Lamp in Paper with `aria-current="page"`. On a phone the same links become the four tab bar: icon over label, no fill, the current tab marked by Paper text and icon.
- **States:** `default`, `current`

### Link status (`link`)
The first item of the status strip: an 8 px dot, a Margin `link` label, and a word. `link` means only the browser's live connection to the dashboard. It starts as `snapshot` (an Edge ring, nothing live without JavaScript), then `connecting` in Signal Indigo, `live` with a Ledger Green dot and Paper word, `reconnecting` in Amber Watch after 5 s without an event, and `lost` in Ledger Red after 15 s. While the link is live but the dashboard cannot read Redis, it reads `no data` in Amber Watch (state `degraded`): a green dot never sits beside dead values. On every event received the dot beats once: a 1 px ring grows from the dot to 2.75 times its size and fades over 700 ms.
- **States:** `static`, `connecting`, `live`, `degraded`, `reconnecting`, `lost`

### Command state (`command`)
The third item of the status strip: a Margin `command` label, the latest command and its outcome in the AC-12 words, and its age as a Margin `· 12s ago` (since it was sent, answered, or expired). Signal Indigo while waiting, Paper when confirmed, Amber Watch when unknown, Ledger Red when failed or expired, Pencil for `none`, and dimmed `no data` when Redis is unreadable.
- **States:** `none`, `pending`, `ok`, `warn`, `bad`, `lost`

### Alert bar (`alert`)
The one place the console raises its voice. A full width line along the bottom of the sticky top bar, on Lamp with a 2 px top rule in Ledger Red (or Amber Watch for a warning), an 18 px Lucide `octagon-alert` (or `triangle-alert`) in the same color, a Label 600 title in that color, and Paper text that says what broke and what to do. It shows only the most severe problem: a stop that did not take effect first, then a silent trader, then a stop the trader has not answered within `DASHBOARD_ACK_WARN_S` (10 s) while its heartbeat is not red (the command state turns Amber Watch with it, and the bar names the send and expiry times), then a dashboard that cannot read Redis. Every stop alert ends with a Pencil line naming the way out when the bot will not stop: close positions on Kraken Futures directly (a text link to the venue for this environment). It is empty, and takes no space, while all is well. Times are absolute through `ts`, never a ticking count, and the bar is a `role="alert"` region, so a screen reader hears each problem once.
- **States:** `stop_unhandled`, `stop_failed`, `stop_late`, `stop_unknown`, `trader_silent`, `stop_unacknowledged`, `no_data`

### Icon (`icon`)
Lucide, drawn inline on a 24 px grid with a 1.5 stroke in the text color around it, 16 px by default. Decorative unless given a label.
- **States:** `default`

### Pulse (`pulse`)
The heartbeat tape, the signature of the console. A 56 px canvas across the full region width shows the trader's real data age once a second for the last 2 minutes (120 slots). Each tick is 1 to 3 px wide and its height is the age on a square root scale, so a steady 1 s still reads as a beat and a stale spike fills the band. Fresh ticks are Pencil, fading toward the left; ticks over the amber threshold are Amber Watch and over red are Ledger Red; the newest fresh tick is Paper. The baseline is a Hairline, dashed in Edge under every second without a live link, so a gap is never invented away. A Caption legend sits at the right of the head and a Mono time axis (`2m ago`, `1m`, `now`) runs under the tape.
- **States:** `default`

### Activity feed (`activity`)
Overview's answer to "what just happened": the newest 20 commands, newest first, as plain rows split by Hairlines, no table head. A run of consecutive commands that read the same (command, outcome) is one row: the command carries a Pencil Mono count (`stop ×16`) and the time a Margin Caption `from` line with the oldest time, so repeats never bury the signal. Each row holds the UTC time (`ts`), the command in Paper at 500 weight, and its outcome at the right edge in the top bar's command colors (Signal Indigo while waiting, Paper when confirmed, Ledger Red when failed or expired, Amber Watch when unknown). A final outcome never hides here. On a phone the time drops under the command. An entry that does not parse is one Amber Watch `unknown entry` row. It dims with the top bar while the link is not live.
- **States:** `empty`, `filled`, `collapsed`, `error`

### Position card (`position`)
Overview's Position and P&L region (spec 0006). An open position leads with its unrealized P&L as a Stat, then label and value rows split by Hairlines: side, size, average entry, the mark in a Stale wrapper, the stop with its status (Amber Watch `no stop` when there is none), the exit time, and equity. Flat shows `Flat` and the mark. Under every state a Caption title `Paper P&L, before funding` heads realized today (UTC), realized all time, and fees today. A missing or red aged position snapshot is the error state `Trader offline`, never `Flat`, so a dead trader never looks calm. A snapshot that does not parse and an unreadable Redis are error states too; a Postgres failure keeps the live part and says the figures are unavailable.
- **States:** `open`, `flat`, `offline`, `bad`, `lost`, `pnl_unavailable`

### Wanted, Trader, Kraken (`sides`)
The top of the position card (spec 0023 AC-18), in every state but `lost`: three label over value columns. *Wanted* is the trader's intent for the instrument from `trader_state.intent` (`hold`, `entry submitted`, `flatten submitted`, `stop replacing`), *Trader* the cache position from the `position` snapshot, and *Kraken* the exchange view from the last check with its age as a Pencil Caption under it. Labels are Margin, values Paper at 500 weight, all split from the figures below by a Hairline. When Trader and Kraken differ in side or size, both values turn Ledger Red at 600 weight and a Ledger Red line says they disagree, so a submitted flatten never looks like a confirmed flat. A missing, failed, or more than three checks old exchange view shows `Kraken unknown` in Ledger Red, never `flat`.
- **States:** `agree`, `disagree`, `unknown`

### Kraken check (`check`)
The reconciliation badge (spec 0023 AC-19), from `trader_state.last_clean_check_ns`, `last_check_outcome`, and the halt. A cue in the status strip, always shown: a Margin `kraken check` label, then `clean · 12s` (the last clean check's age) in Pencil. `checking` before the first check finishes is Amber Watch at 600 weight. `mismatch`, `halted`, and `Kraken unknown` (the last fetch failed) are Ledger Red at 600 weight, the alert colour. A missing `trader_state` shows `no data`, dimmed. It dims with the top bar while the link is not live. It links to the Reconciliation page, a Table of every reported check (newest first, 50 per page) with each finding and its action in plain words; an outcome is a Badge, `clean` in Ledger Green, `mismatch` and `failed` in Ledger Red.
- **States:** `clean`, `checking`, `mismatch`, `halted`, `unknown`, `lost`

### Entries blocked (`blocked`)
Why the trader takes no new entries (spec 0006 AC-18), from `trader_state.entries_blocked`. In the top bar a cue in the status strip, like the pause cue: a Margin `entries` label, then `blocked` in Amber Watch at 600 weight, with `· n reasons` when there is more than one and the plain words in its title. On Overview, above the position card, an Amber Watch title `No new entries` heads one row per reason split by Hairlines: the plain words of the spec's *Block reasons* table (those that only clear on a restart say so), with the code as a Pencil Caption at the right. Nothing renders while entries are open. A missing or unreadable `trader_state` shows nothing here, since the pause cue and the heartbeat age already say the state is unknown. Both dim with the top bar while the link is not live.
- **States:** `none`, `one`, `several`

## Do's and Don'ts

### Do:
- **Do** run every number through `num`, `pnl`, or `ts`, and pass `Decimal` values only.
- **Do** show every Redis value inside a `stale` wrapper or next to its age.
- **Do** keep the stop button at the right edge of the top bar of every page, alone behind a 1 px rule, at least 44 × 44 px.
- **Do** add a new color combination to `contrast_pairs` before using it.
- **Do** separate regions with 1 px seams over Hairline, on the board grid.
- **Do** keep motion to 120 ms ease out state changes, one beat per real event, and the busy and loading indicators; nothing moves under `prefers-reduced-motion`.

### Don't:
- **Don't** use shadows, glows, gradients, blurs, or cards; use the three graphite steps and 1 px rules.
- **Don't** color anything that is not reporting a state, and don't fill a button with Signal Indigo.
- **Don't** use a `float` for money, or show a local time outside a tooltip.
- **Don't** write inline `style` or `script`, or `hx-on` attributes; the CSP blocks them.
- **Don't** load anything from another origin: fonts, icons, and libraries are self hosted.
- **Don't** build a grid of KPI tiles; figures sit in regions and tables.
- **Don't** set a colored side border thicker than 1 px on notices or rows.
