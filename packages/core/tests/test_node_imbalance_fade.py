"""covers: spec 0021 AC-3 (``ImbalanceFade`` on the node: it builds an ``ImbalanceFadeState``, its config keeps
``BookImbalanceConfig``'s defaults, and the book ``book_imbalance`` joins long is entered short, with the
``imbalance_fade_entry`` reason, on the same aligned firing).

Reuses the ``BookImbalance`` engine harness. Needs the ``node`` extra.
"""

import pytest

pytest.importorskip("nautilus_trader")

from test_node_book_imbalance import _buys, _run, _snapshot
from test_node_placeholder import START, _ns
from test_node_scalp_base import _decisions

from trading_core.node.book_imbalance import BookImbalanceConfig
from trading_core.node.imbalance_fade import ImbalanceFade, ImbalanceFadeConfig
from trading_core.recording.enums import Intent, OrderSide
from trading_core.strategy.book_imbalance import BUCKET_NS
from trading_core.strategy.imbalance_fade import ImbalanceFadeState


def test_the_config_keeps_book_imbalances_fields_and_defaults():
    assert issubclass(ImbalanceFadeConfig, BookImbalanceConfig)
    assert ImbalanceFadeConfig.__struct_fields__ == BookImbalanceConfig.__struct_fields__
    assert ImbalanceFadeConfig.__struct_defaults__ == BookImbalanceConfig.__struct_defaults__
    assert ImbalanceFadeConfig.__struct_config__.frozen


def test_a_heavy_book_with_buying_flow_is_entered_short_on_the_second_aligned_firing():
    _, strategy, rows, _ = _run(
        2,
        book=_snapshot("3.0000", "1.0000"),
        trades=_buys(120),
        strategy_cls=ImbalanceFade,
        config_cls=ImbalanceFadeConfig,
    )

    assert isinstance(strategy.rule, ImbalanceFadeState)
    assert strategy.entry_reason == "imbalance_fade_entry"
    enters = _decisions(rows, Intent.ENTER)
    assert enters, "no enter decision"
    assert enters[0].side == OrderSide.SELL
    assert enters[0].reason == "imbalance_fade_entry"
    assert enters[0].inputs["imbalance"] == "0.5"
    assert enters[0].decided_ns == _ns(START) + 2 * BUCKET_NS


def test_an_empty_ask_side_enters_nothing():
    _, _, rows, fired = _run(
        2,
        book=_snapshot("3.0000", None),
        trades=_buys(120),
        strategy_cls=ImbalanceFade,
        config_cls=ImbalanceFadeConfig,
    )

    assert len(fired) >= 7
    assert _decisions(rows, Intent.ENTER) == []
