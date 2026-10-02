---
name: Kraken Futures Scalper
description: A calm, dark instrument panel for one trader watching an automated scalper, with a stop that is never more than one tap away.
colors:
  bg: "#0B0D10"
  surface: "#12151A"
  raised: "#181C22"
  rule: "#232831"
  edge: "#646D7A"
  text: "#E6E8EB"
  muted: "#9AA3AE"
  faint: "#838C98"
  profit: "#2FBF71"
  loss: "#F0524F"
  warn: "#E8A33D"
  info: "#6CA6FF"
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
  button-danger:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.loss}"
    rounded: "{rounded.4}"
    height: 44px
  button-stop:
    backgroundColor: "{colors.stop}"
    textColor: "{colors.on-alarm}"
    rounded: "{rounded.4}"
    height: 44px
  button-primary:
    backgroundColor: "{colors.info}"
    textColor: "{colors.bg}"
    rounded: "{rounded.4}"
    height: 44px
  badge-demo:
    backgroundColor: "{colors.demo}"
    textColor: "{colors.bg}"
    typography: "{typography.caption}"
    rounded: "{rounded.2}"
  badge-live:
    backgroundColor: "{colors.live}"
    textColor: "{colors.on-alarm}"
    typography: "{typography.caption}"
    rounded: "{rounded.2}"
  input:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.text}"
    typography: "{typography.number}"
    rounded: "{rounded.4}"
    height: 44px
  table-cell:
    textColor: "{colors.text}"
    typography: "{typography.number}"
    padding: "{spacing.8} {spacing.12}"
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
---

# Design System: Kraken Futures Scalper

## Overview

**Creative North Star: "The Night Desk"**

A trading desk after hours: the room is dark, the screens are quiet, and the only light that moves is light that means something. This dashboard is an instrument panel for one person supervising a bot, not a terminal for placing trades. It reads on a laptop at a desk and on a phone in a hand, and at both sizes the same three facts come first: which environment this is, how fresh the picture is, and where the stop is.

Calm is the default and the point. Neutral grays carry structure; color is reserved for meaning (profit, loss, stale, live, stop), so when color appears it is information, never decoration. Density is welcome: numbers sit in tight, aligned columns because the trader reads them as columns. Depth comes from thin rules and three tonal surfaces, never from shadows or cards.

The design follows `PRODUCT.md`: freshness is part of every number, the stop is never slower than panic, and the screen stays calm until something is wrong, then becomes unmissable.

**Key Characteristics:**
- Dark only, three surface tones, 1 px rules instead of cards.
- IBM Plex Sans for words, IBM Plex Mono with tabular figures for every number.
- Color only for meaning; gray for everything else.
- Every live value carries its age or a `link lost` marker.
- A red stop button in the top bar of every page, 44 × 44 px at least.

## Colors

A near black, slightly cool neutral ramp with a small, strict set of signal colors.

### Primary
- **Signal Blue** (info): focus rings, the primary login action, links, and a command that is in flight. The only accent used for interaction.

### Secondary
- **Ledger Green** (profit): positive P&L and healthy status dots. Never used for buttons or decoration.
- **Ledger Red** (loss): negative P&L, failures, `no heartbeat`, `Stop NOT sent`.
- **Amber Watch** (warn): stale data (above 3 s), unknown results, warnings.

### Tertiary
- **Live Red** (live): the LIVE badge and the 2 px top border of the bar in live. Means "real money".
- **Demo Amber** (demo): the DEMO badge. Same hue family as warn on purpose: demo is a caution, not a state of calm.
- **Stop Crimson** (stop): the stop button only. White (on-alarm) text, 600 weight.

### Neutral
- **Night** (bg): the page.
- **Desk** (surface): the top bar, side panels, the login panel.
- **Lamp** (raised): buttons, table header, hovered rows, notices.
- **Hairline** (rule): dividers between regions and rows. Never the only edge of a control.
- **Edge** (edge): the border of inputs and default buttons, at 3:1 or better against every surface.
- **Paper** (text): primary text and numbers.
- **Pencil** (muted): secondary text, empty states.
- **Margin** (faint): labels only (`trader`, `command`, column heads), never values.
- **On alarm** (on-alarm): white text on stop and live.

### Named Rules
**The Meaning Rule.** Every color other than the neutrals means a state. If an element is colored and you cannot name the state it reports, it should be gray.

**The Pair Rule.** Every text and background combination in use is listed in `contrast_pairs` and passes WCAG AA; a new combination is added there first, and the token test enforces it.

## Typography

**Body Font:** IBM Plex Sans (with system-ui, sans-serif)
**Number Font:** IBM Plex Mono (with ui-monospace, monospace), always with tabular figures

**Character:** Plex is an engineer's typeface: plain, sturdy, slightly mechanical. The mono cut is used for data and measurement only, where equal width digits let columns line up; words stay in the sans.

### Hierarchy
- **Display** (600, 32 px, 1.2): reserved; not used in the shell yet.
- **Headline** (600, 24 px, 1.25): rare section headings.
- **Title** (600, 20 px, 1.3): the page title.
- **Body large** (400, 16 px, 1.5): login inputs and copy that must read easily on a phone.
- **Body** (400, 14 px, 1.45): default text.
- **Label** (500, 13 px, 1.4): top bar items, buttons, form labels, table headers.
- **Caption** (500, 12 px, 0.06em, uppercase): badges and column group labels.
- **Number** (Mono 400, 14 px, tabular): every number in a table or a line.
- **Number large** (Mono 500, 20 px, tabular): the value in a stat.

### Numbers and time
These rules are implemented by the `num`, `pnl`, and `ts` template filters; templates never format numbers by hand.
- **Decimal only.** `num`, `pnl` refuse a `float`, `int`, or `bool` (TypeError) and NaN or infinity (ValueError). Callers quantize to the instrument's precision; the filters never round.
- **Own precision.** A value shows exactly the digits its `Decimal` carries: `1234.50` → `1,234.50`, `1E+3` → `1,000`.
- **Grouping and sign.** Comma thousands, dot decimal. Negatives use the true minus sign `−` (U+2212), which matches the plus sign in width. Negative zero shows as `0`.
- **P&L.** `pnl` adds `+` or `−`, ▲ or ▼, and the profit or loss color. Zero is neutral, with no sign and no arrow. The arrow is hidden from screen readers; the sign carries the meaning.
- **Time.** Every timestamp is UTC, truncated to seconds, written `2026-10-02 14:03:07Z`. Amsterdam local time appears only in the `title` tooltip (`16:03:07 CEST, Amsterdam`).
- **Right aligned.** Numbers in tables align right so digits of the same place line up.

### Named Rules
**The Mono for Measurement Rule.** Mono is for numbers, ids, and codes. A word set in mono is a costume.

## Layout

A fixed top bar holds the environment badge, the trader's data age, the last command's state, and the stop button; the page sits under it. The top bar wraps onto two lines on a phone but the stop button never leaves the first viewport. Content regions are separated by 1 px rules and 24 px gaps, not boxes. Spacing follows a 4 px base (4, 8, 12, 16, 24, 32, 48): 8 inside groups, 16 between related groups, 24 to 32 between regions. Pages use a 16 px gutter on a phone and 24 px from 900 px wide; content stops at 1200 px. Tables scroll inside their own container on narrow screens; the page never scrolls sideways.

## Elevation & Depth

No shadows anywhere. Depth is tonal: Night (page) under Desk (bars and panels) under Lamp (controls and headers), with Hairline rules where two regions meet. A sticky element (the top bar, a table header) gets its own surface tone and a rule along its edge, which is enough to separate it from content scrolling beneath.

### Named Rules
**The Flat Desk Rule.** If something needs to stand out, give it a different surface tone or a rule, never a shadow or a glow.

## Shapes

Corners are nearly square: 2 px for badges and notices, 4 px for buttons, inputs, and panels. Nothing is pill shaped except status dots, which are circles. Borders are 1 px; the only thicker line is the 2 px red top border of the bar in live.

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
Small, uppercase caption on a solid tone, 2 px corners. The env badge is the loudest object in the top bar on purpose; status badges are quiet outlines.
- **States:** `demo`, `live`, `ok`, `warn`, `bad`, `neutral`

### Status dot (`status_dot`)
An 8 px circle beside a text label; never the only signal.
- **States:** `ok`, `warn`, `bad`, `off`

### Stat (`stat`)
A faint label over a large number, aligned to a grid, no box around it.
- **States:** `default`, `stale`, `error`, `loading`

### Table (`table`)
Dense rows (8 px by 12 px cells), a sticky Lamp header with faint uppercase captions, rules between rows, numbers right aligned, a row hover in Lamp.
- **States:** `default`, `empty`, `loading`

### Button (`button`)
- **Shape:** 4 px corners, 44 px tall, label weight.
- **Default:** Lamp fill, Edge border, Paper text. **Hover:** border turns Pencil. **Focus:** 2 px Signal Blue outline, 2 px offset.
- **Quiet:** no fill and no border, Pencil text, underline on hover. For secondary actions in the top bar (Log out), set apart from the stop by a Hairline rule.
- **Danger:** Lamp fill, Ledger Red text and border, for destructive actions that are not the stop.
- **Stop:** Stop Crimson fill, white 600 text, always visible, never disabled by the client.
- **Disabled:** 50 percent opacity, no hover, `not-allowed` cursor.
- **Busy:** the label stays, a thin moving rule under it shows work in progress, and the button ignores clicks.
- **States:** `default`, `quiet`, `danger`, `stop`, `disabled`, `busy`

### Empty state (`empty_state`)
One sentence in Pencil saying what will appear here and when, inside a dashed Hairline frame.
- **States:** `default`

### Loading (`loading`)
Skeleton bars in Lamp that pulse gently (static under reduced motion); never a spinner in the middle of content.
- **States:** `default`

### Error state (`error_state`)
Names the problem and the recovery in Ledger Red with a 1 px border, not a banner.
- **States:** `default`

### Stale wrapper (`stale`)
Wraps any live value with its age. Fresh shows the value plainly; above the amber threshold the age turns Amber Watch; above red it turns Ledger Red; with the link lost the value dims to 60 percent and reads `link lost`, so a frozen number never looks live.
- **States:** `ok`, `warn`, `bad`, `lost`

### Chart (`chart`)
A Lightweight Charts canvas themed from the tokens: Night background, Hairline grid, Pencil axis text in Plex Mono, Ledger Green and Red for up and down. A `Charting by TradingView` text link sits under each chart (the library's logo needs an inline style the CSP forbids).
- **States:** `default`

### Navigation
Arrives with the complete shell (spec 0004 milestone 4): a side column from 900 px wide, a bottom tab bar below.

## Do's and Don'ts

### Do:
- **Do** run every number through `num`, `pnl`, or `ts`, and pass `Decimal` values only.
- **Do** show every Redis value inside a `stale` wrapper or next to its age.
- **Do** keep the stop button in the top bar of every page, at least 44 × 44 px.
- **Do** add a new color combination to `contrast_pairs` before using it.
- **Do** keep motion to 120 ms ease out state changes, and none under `prefers-reduced-motion`.

### Don't:
- **Don't** use shadows, glows, gradients, or cards; use surface tones and 1 px rules.
- **Don't** color anything that is not reporting a state.
- **Don't** use a `float` for money, or show a local time outside a tooltip.
- **Don't** write inline `style` or `script`, or `hx-on` attributes; the CSP blocks them.
- **Don't** load anything from another origin: fonts and libraries are self hosted.
- **Don't** set a colored side border thicker than 1 px on notices or rows.
