"""covers: spec 0023 AC-14 (an entry with no answer from Kraken is unresolved until answered; one Nautilus's in
flight check rejected stays unresolved until a clean check). Pure, no Nautilus."""

from trading_core.reconcile.unresolved import INFLIGHT_REJECTS, UnresolvedEntries

BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"


def test_an_entry_is_unresolved_from_its_submit_until_kraken_answers():
    assert not UnresolvedEntries().active
    entries = UnresolvedEntries()

    entries.submitted("O-1", BTC)
    assert entries.active
    assert entries.instruments == frozenset({BTC})

    entries.answered("O-1")
    assert not entries.active


def test_a_rejection_from_kraken_is_an_answer():
    entries = UnresolvedEntries()
    entries.submitted("O-1", BTC)

    entries.rejected("O-1", "insufficientAvailableFunds")

    assert not entries.active


def test_an_in_flight_check_rejection_stays_unresolved_until_a_clean_check():
    entries = UnresolvedEntries()
    entries.submitted("O-1", BTC)
    entries.submitted("O-2", ETH)

    entries.rejected("O-1", "UNKNOWN")
    entries.answered("O-2")
    entries.answered("O-1")  # a later event for it changes nothing: Kraken may still hold it

    assert entries.active
    assert entries.instruments == frozenset({BTC})
    entries.clear()
    assert not entries.active


def test_both_in_flight_reasons_count():
    assert frozenset({"UNKNOWN", "ORDER_NOT_FOUND_AT_VENUE"}) == INFLIGHT_REJECTS
    entries = UnresolvedEntries()
    entries.submitted("O-1", BTC)
    entries.rejected("O-1", "ORDER_NOT_FOUND_AT_VENUE")
    assert entries.active


def test_a_rejection_of_an_order_never_submitted_is_ignored():
    entries = UnresolvedEntries()

    entries.rejected("O-9", "UNKNOWN")

    assert not entries.active
