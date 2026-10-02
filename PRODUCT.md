# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One trader, the owner of this project, based in the Netherlands, who builds and runs the bot alone. At a desk they use a laptop; away from it they use a phone, often at the moment something is going wrong.

## Product Purpose

A private window on an automated Kraken Futures scalper (BTC and ETH perpetuals). At a glance it answers: is this DEMO or LIVE, is the data fresh, what is the position and P&L, and what happened to my last command. It also gives a stop that works with one tap. Success means the trader never acts on a stale or wrong picture.

## Positioning

Not a trading terminal. Orders are never placed here: the bot trades, and the trader supervises it and stops it.

## Operating Context

- Reached only over Tailscale, behind a password plus a 6 digit authenticator code.
- The same app serves the laptop and the phone.
- The bot runs 24/7 on a London VPS; a laptop stack runs the demo environment for development.

## Capabilities and Constraints

- No CDN and no third party requests; a strict Content Security Policy (no inline script or style).
- The stop button must work without JavaScript.
- Money is `Decimal`, never a float; every time shown is UTC.
- Read only, except the stop command sent to the trader.
- Dark only for now.

## Brand Commitments

- The look is the category standard played straight, at the craft level of Linear and Hyperliquid: sleek, dark, quiet, precise. Chosen over three rolls of conceptual directions (2026-10-02).

## Evidence on Hand

None yet: no real trades exist. Never fabricate P&L, fills, or strategy scores. Sample data, such as on `/styleguide`, is labelled as a sample.

## Product Principles

1. Freshness is part of every number: a value without its age is not shown.
2. The stop is never slower than panic.
3. Calm until something is wrong; then unmissable.
4. Dense and scannable over decorative.

## Accessibility & Inclusion

WCAG AA contrast, 44 px touch targets, and `prefers-reduced-motion` respected.
