"""covers: spec 0023 AC-18, the three state position card: Wanted (``trader_state.intent``), Trader (the
coin's ``positions`` entry), and Kraken (the ``exchange`` snapshot with its age); a disagreement in side or size
shows in the alert colour, and a missing, failed, or stale exchange view reads ``Kraken unknown``, never flat.
Spec 0025 AC-21: each coin row gets its own three."""

from decimal import Decimal

import pytest

from dashboard.live.sides import KRAKEN_UNKNOWN, SidesView, sides_view
from trading_core.snapshot.envelope import encode

from .conftest import log_in

S = 1_000_000_000
NOW = 1_790_000_000 * S
BTC = "PF_XBTUSD.KRAKEN"
PEPE = "PF_PEPEUSD.KRAKEN"
INTERVAL_S = 60.0
LONG = {"instrument_id": BTC, "base": "BTC", "side": "long", "quantity": "0.0011", "mark_px": "64288.5"}
HOLD_LONG = ("long", Decimal("0.0011"))
HOLD_FLAT = ("flat", Decimal(0))


def _exchange(positions=(), age_s=12, status="ok"):
    data = {"status": status, "fetched_ns": NOW - age_s * S}
    if status == "ok":
        data |= {"positions": list(positions), "orders": []}
    return encode(NOW - age_s * S, data)


def _held(side="long", quantity="0.0011", instrument_id=BTC):
    return {"instrument_id": instrument_id, "side": side, "quantity": quantity}


def _state(intent=None):
    return encode(NOW, {"paused": False, "intent": {} if intent is None else intent})


def _view(holding=HOLD_LONG, exchange=None, state=None):
    return sides_view(
        BTC,
        holding,
        _exchange([_held()]) if exchange is None else exchange,
        _state() if state is None else state,
        NOW,
        INTERVAL_S,
    )


def test_agreeing_views_show_all_three_with_the_kraken_age():
    assert _view() == SidesView("hold", "long 0.0011", "long 0.0011", "12s", "agree")


def test_the_same_size_written_differently_still_agrees():
    assert _view(exchange=_exchange([_held(quantity="0.00110")])).state == "agree"


def test_flat_on_both_sides_agrees_and_kraken_flat_lists_no_position():
    view = _view(HOLD_FLAT, _exchange([]))

    assert (view.trader, view.kraken, view.state) == ("flat", "flat", "agree")


@pytest.mark.parametrize(
    ("exchange", "kraken"),
    [
        (_exchange([]), "flat"),
        (_exchange([_held(side="short")]), "short 0.0011"),
        (_exchange([_held(quantity="0.0005")]), "long 0.0005"),
    ],
)
def test_a_different_side_or_size_disagrees(exchange, kraken):
    view = _view(exchange=exchange)

    assert (view.trader, view.kraken, view.state) == ("long 0.0011", kraken, "disagree")


def test_a_position_on_another_instrument_is_not_this_card():
    view = _view(exchange=_exchange([_held(instrument_id="PF_ETHUSD.KRAKEN")]))

    assert (view.kraken, view.state) == ("flat", "disagree")


@pytest.mark.parametrize(
    "exchange",
    [
        None,
        _exchange(status="unknown"),
        _exchange([_held()], age_s=int(3 * INTERVAL_S) + 1),
        b"not json",
        encode(NOW, {"status": "ok", "fetched_ns": NOW, "positions": "nope"}),
        encode(NOW, {"status": "ok", "fetched_ns": NOW, "positions": [_held(quantity="lots")]}),
    ],
)
def test_a_missing_failed_stale_or_unreadable_exchange_view_is_kraken_unknown_never_flat(exchange):
    view = sides_view(BTC, HOLD_LONG, exchange, _state(), NOW, INTERVAL_S)

    assert (view.kraken, view.kraken_age, view.state) == (KRAKEN_UNKNOWN, None, "unknown")


def test_three_intervals_old_is_still_known():
    assert _view(exchange=_exchange([_held()], age_s=int(3 * INTERVAL_S))).state == "agree"


@pytest.mark.parametrize(
    ("intent", "wanted"),
    [
        ({BTC: "entry_submitted"}, "entry submitted"),
        ({BTC: "flatten_submitted"}, "flatten submitted"),
        ({BTC: "stop_replacing"}, "stop replacing"),
        ({BTC: "hold"}, "hold"),
        ({"PF_ETHUSD.KRAKEN": "entry_submitted"}, "hold"),
    ],
)
def test_wanted_is_the_intent_for_the_cards_instrument(intent, wanted):
    assert _view(state=_state(intent)).wanted == wanted


def test_a_flatten_submitted_but_not_confirmed_never_looks_flat():
    view = _view(exchange=_exchange([_held()]), state=_state({BTC: "flatten_submitted"}))

    assert (view.wanted, view.trader, view.kraken) == ("flatten submitted", "long 0.0011", "long 0.0011")


def test_without_trader_state_wanted_is_unknown():
    view = sides_view(BTC, HOLD_LONG, _exchange([_held()]), None, NOW, INTERVAL_S)

    assert view.wanted == "unknown"


def test_an_unknown_trader_view_never_disagrees():
    view = sides_view(BTC, None, _exchange([_held(side="short")]), _state(), NOW, INTERVAL_S)

    assert view.trader == "unknown"
    assert view.state == "agree"


def test_without_a_position_snapshot_kraken_shows_its_one_position():
    view = sides_view(None, None, _exchange([_held()]), _state(), NOW, INTERVAL_S)

    assert (view.wanted, view.kraken) == ("unknown", "long 0.0011")


# The rendered card ---------------------------------------------------------------------------------


def _overview(client, clock, redis, *, exchange=True, kraken_side="long", intent=None, coins=None):
    now = clock.ns()
    coins = [{**LONG, **_OPEN_FIELDS}] if coins is None else coins
    redis.values["demo:snapshot:positions"] = encode(now - S, {"coins": coins})
    redis.values["demo:snapshot:trader_state"] = encode(now, {"paused": False, "intent": intent or {}})
    if exchange:
        redis.values["demo:snapshot:exchange"] = encode(
            now - 5 * S,
            {"status": "ok", "fetched_ns": now - 5 * S, "positions": [_held(side=kraken_side)], "orders": []},
        )
    log_in(client, clock)
    return client.get("/").text


_OPEN_FIELDS = {
    "avg_entry_px": "64210.50",
    "unrealized_pnl": "0.09",
    "stop_px": "64018",
    "stop_status": "accepted",
    "opened_ns": NOW,
    "exit_due_ns": None,
    "currency": "USD",
}


def test_overview_shows_wanted_trader_and_kraken(client, clock, redis):
    page = _overview(client, clock, redis, intent={BTC: "flatten_submitted"})

    assert 'class="sides sides--agree"' in page
    assert "<dt>Wanted</dt><dd>flatten submitted</dd>" in page
    assert (
        '<dt>Kraken</dt><dd class="sides__value sides__kraken">long 0.0011<span class="sides__age">5s old</span>'
        in page
    )
    assert "Trader and Kraken disagree" not in page


def test_overview_says_when_trader_and_kraken_disagree(client, clock, redis):
    page = _overview(client, clock, redis, kraken_side="short")

    assert 'class="sides sides--disagree"' in page
    assert "Trader and Kraken disagree" in page


def test_overview_without_an_exchange_snapshot_says_kraken_unknown(client, clock, redis):
    page = _overview(client, clock, redis, exchange=False)

    assert 'class="sides sides--unknown"' in page
    assert f'sides__kraken">{KRAKEN_UNKNOWN}</dd>' in page


def test_each_coin_row_gets_its_own_wanted_trader_and_kraken(client, clock, redis):
    pepe_flat = {"instrument_id": PEPE, "base": "PEPE", "side": "flat", "mark_px": "0.0000123456"}
    page = _overview(
        client, clock, redis, coins=[{**LONG, **_OPEN_FIELDS}, pepe_flat], intent={PEPE: "entry_submitted"}
    )

    assert page.count('class="sides sides--agree"') == 2
    assert "<dt>Wanted</dt><dd>hold</dd>" in page
    assert "<dt>Wanted</dt><dd>entry submitted</dd>" in page
    assert 'sides__kraken">long 0.0011<span' in page
    assert 'sides__kraken">flat<span' in page


def test_a_card_without_rows_shows_one_set_for_no_coin(client, clock, redis):
    redis.values["demo:snapshot:exchange"] = encode(
        clock.ns() - 5 * S, {"status": "ok", "fetched_ns": clock.ns() - 5 * S, "positions": [_held()], "orders": []}
    )
    log_in(client, clock)
    page = client.get("/").text

    assert "Trader offline" in page
    assert page.count('class="sides ') == 1
    assert 'sides__kraken">long 0.0011<span' in page
