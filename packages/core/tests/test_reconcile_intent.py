"""covers: spec 0023 AC-18, the trader's intent per instrument (*Wanted* on the position card), pure."""

from trading_core.reconcile.intent import Intent, Intents, order_intent

BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"


def test_an_entry_is_entry_submitted_an_exit_is_flatten_submitted_and_a_stop_changes_nothing():
    assert order_intent(reduce_only=False, is_stop=False) is Intent.ENTRY_SUBMITTED
    assert order_intent(reduce_only=True, is_stop=False) is Intent.FLATTEN_SUBMITTED
    assert order_intent(reduce_only=True, is_stop=True) is None


def test_nothing_seen_yet_shows_no_instrument():
    assert Intents().by_instrument() == {}


def test_an_intent_lasts_until_its_order_ends_then_the_instrument_holds():
    intents = Intents()
    intents.start("O-1", BTC, Intent.ENTRY_SUBMITTED)
    assert intents.by_instrument() == {BTC: "entry_submitted"}

    intents.end("O-1")

    assert intents.by_instrument() == {BTC: "hold"}


def test_the_newest_open_intent_shows_and_an_older_one_shows_again_when_the_newer_ends():
    intents = Intents()
    intents.start("O-1", BTC, Intent.ENTRY_SUBMITTED)
    intents.start("fix", BTC, Intent.STOP_REPLACING)
    assert intents.by_instrument() == {BTC: "stop_replacing"}

    intents.end("fix")

    assert intents.by_instrument() == {BTC: "entry_submitted"}


def test_instruments_are_kept_apart_and_an_unknown_key_is_ignored():
    intents = Intents()
    intents.start("O-1", BTC, Intent.FLATTEN_SUBMITTED)
    intents.start("O-2", ETH, Intent.ENTRY_SUBMITTED)
    intents.end("O-9")
    intents.end("O-2")

    assert intents.by_instrument() == {BTC: "flatten_submitted", ETH: "hold"}
