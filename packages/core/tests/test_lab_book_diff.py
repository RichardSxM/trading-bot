"""covers: spec 0008 AC-1 (a mid day Tardis snapshot block as level changes against the carried book)."""

from decimal import Decimal

import pytest

from trading_core.lab.book_diff import Level, LevelChange, diff_book

D = Decimal


def bid(price, size):
    return Level("bid", D(price), D(size))


def ask(price, size):
    return Level("ask", D(price), D(size))


def apply(book, changes):
    levels = {(lv.side, lv.price): lv.size for lv in book}
    for change in changes:
        if change.action == "delete":
            del levels[(change.side, change.price)]
        else:
            levels[(change.side, change.price)] = change.size
    return {Level(side, price, size) for (side, price), size in levels.items()}


def test_a_block_that_changes_removes_and_adds_levels():
    carried = [bid("100", "1"), bid("99", "2"), bid("98", "3"), ask("101", "1"), ask("102", "2")]
    block = [bid("100", "1"), bid("99", "5"), bid("97", "4"), ask("101", "1"), ask("103", "6")]

    changes = diff_book(carried, block)

    assert changes == [
        LevelChange("update", "bid", D("99"), D("5")),
        LevelChange("delete", "bid", D("98"), D("0")),
        LevelChange("add", "bid", D("97"), D("4")),
        LevelChange("delete", "ask", D("102"), D("0")),
        LevelChange("add", "ask", D("103"), D("6")),
    ]
    assert apply(carried, changes) == set(block)


def test_a_block_equal_to_the_carried_book_gives_nothing():
    book = [bid("100", "1"), ask("101", "2")]

    assert diff_book(book, list(reversed(book))) == []


def test_an_empty_carried_book_gives_every_level_as_an_add():
    block = [ask("101", "1"), bid("99", "2"), bid("100", "1")]

    changes = diff_book([], block)

    assert [(c.action, c.side, c.price) for c in changes] == [
        ("add", "bid", D("100")),
        ("add", "bid", D("99")),
        ("add", "ask", D("101")),
    ]


def test_a_level_without_size_or_a_price_twice_is_refused():
    with pytest.raises(ValueError, match="no size"):
        diff_book([bid("100", "0")], [])
    with pytest.raises(ValueError, match="two bid levels"):
        diff_book([], [bid("100", "1"), bid("100", "2")])
