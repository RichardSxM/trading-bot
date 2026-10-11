"""covers: spec 0007 AC-2 (one instrument row per instrument and finished hour), AC-5 (any single hour or
day carries its own definition), AC-11 (rereads and reruns add nothing twice).

The instrument file holds the whole venue's list and is refreshed every 60 minutes, so these tests
drive ``InstrumentDefinitions`` directly with small instrument files."""

import logging

import pyarrow.parquet as pq
from feed_fixture import H0, HOUR_NS, INSTRUMENT, SECOND_NS, write_instruments
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from recorder.compactor.instruments import InstrumentDefinitions
from recorder.compactor.store import DELTAS_TABLE, INSTRUMENTS_TABLE, TRADES_TABLE, CatalogStore, stream_key
from recorder.live_files import FeatherFile

RUN = "a-run"
ETH = "ETHUSDT-PERP.BINANCE"
STREAMS = (DELTAS_TABLE, TRADES_TABLE)
OURS = stream_key(INSTRUMENTS_TABLE, INSTRUMENT)


def _btc(ts, **changes):
    """The BTC definition stamped at ``ts``, with any fields changed."""
    definition = CryptoPerpetual.to_dict(TestInstrumentProvider.btcusdt_perp_binance())
    return CryptoPerpetual.from_dict({**definition, "ts_event": ts, "ts_init": ts, **changes})


def _eth(ts):
    definition = CryptoPerpetual.to_dict(TestInstrumentProvider.ethusdt_perp_binance())
    return CryptoPerpetual.from_dict({**definition, "ts_event": ts, "ts_init": ts})


def _file(store, instruments, created_ns=H0):
    path = write_instruments(store.live, RUN, instruments, created_ns=created_ns)
    return FeatherFile.parse(path, RUN)


def _finished(done, *hours, table=DELTAS_TABLE, instrument=INSTRUMENT):
    """Mark stream hours as converted, as the compactor does before it writes instrument rows."""
    for hour in hours:
        done.hours.setdefault(stream_key(table, instrument), {})[hour] = {"rows": 10, "bytes": 100}


def _row(store, hour, instrument=INSTRUMENT):
    table = pq.read_table(store.data_file(INSTRUMENTS_TABLE, instrument, hour))
    return table.num_rows, ArrowSerializer.deserialize(CryptoPerpetual, table.to_batches()[0])[0]


def _missing(caplog):
    return [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == "instrument_definition_missing"]


def _at(defs, ts, done):
    """The definition at ``ts``, which these tests always expect to exist."""
    definition = defs.definition_at(INSTRUMENT, ts, done)
    assert definition is not None
    return definition


def test_only_the_wanted_instruments_are_kept(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)

    defs.read(_file(store, [_btc(H0), _eth(H0)]), {INSTRUMENT})

    done = store.load_done()
    assert defs.definition_at(INSTRUMENT, H0, done) is not None
    assert defs.definition_at(ETH, H0, done) is None


def test_reading_a_file_again_reads_only_rows_added_since(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    first = _file(store, [_btc(H0)])
    defs.read(first, {INSTRUMENT})
    defs.read(first, {INSTRUMENT})
    rel = store.relative(first.path)
    assert defs.newest_ts[rel] == H0

    # The open file grows: the recorder appends the next hourly refresh, with a changed field.
    grown = _file(store, [_btc(H0), _btc(H0 + HOUR_NS, max_quantity="5000.000")])
    defs.read(grown, {INSTRUMENT})

    done = store.load_done()
    assert defs.newest_ts[rel] == H0 + HOUR_NS
    assert _at(defs, H0 + HOUR_NS, done)["max_quantity"] == "5000.000"
    assert _at(defs, H0 + HOUR_NS - 1, done)["max_quantity"] != "5000.000"


def test_an_unchanged_hourly_refresh_adds_no_new_definition(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)

    defs.read(_file(store, [_btc(H0), _btc(H0 + HOUR_NS), _btc(H0 + 2 * HOUR_NS)]), {INSTRUMENT})

    definition = _at(defs, H0 + 3 * HOUR_NS, store.load_done())
    assert definition["ts_init"] == H0  # still the first one, the repeats were dropped


def test_the_newest_definition_at_or_before_the_end_is_used(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    change = H0 + HOUR_NS + 30 * 60 * SECOND_NS
    defs.read(_file(store, [_btc(H0), _btc(change, max_quantity="5000.000")]), {INSTRUMENT})
    done = store.load_done()

    assert _at(defs, change - 1, done)["ts_init"] == H0
    assert _at(defs, change, done)["ts_init"] == change


def test_without_a_read_definition_the_carried_one_is_used_else_none(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    done = store.load_done()
    assert defs.definition_at(INSTRUMENT, H0, done) is None

    carried = CryptoPerpetual.to_dict(_btc(H0 - HOUR_NS))
    done.instruments[INSTRUMENT] = carried

    assert defs.definition_at(INSTRUMENT, H0, done) == carried


def test_each_finished_stream_hour_gets_one_row_stamped_with_its_hour(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    defs.read(_file(store, [_btc(H0 - SECOND_NS)]), {INSTRUMENT})
    done = store.load_done()
    _finished(done, H0)
    _finished(done, H0 + HOUR_NS, table=TRADES_TABLE)  # a trades only hour counts too

    wrote = defs.write_hours({INSTRUMENT}, done, STREAMS)

    assert wrote is True
    assert sorted(done.hours[OURS]) == [H0, H0 + HOUR_NS]
    for hour in (H0, H0 + HOUR_NS):
        rows, instrument = _row(store, hour)
        assert rows == 1
        assert instrument.id.value == INSTRUMENT
        assert instrument.ts_init == hour
        path = store.data_file(INSTRUMENTS_TABLE, INSTRUMENT, hour)
        assert done.hours[OURS][hour] == {"rows": 1, "bytes": path.stat().st_size}
    assert done.instruments[INSTRUMENT]["id"] == INSTRUMENT


def test_a_second_write_adds_nothing(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    defs.read(_file(store, [_btc(H0)]), {INSTRUMENT})
    done = store.load_done()
    _finished(done, H0)
    defs.write_hours({INSTRUMENT}, done, STREAMS)
    path = store.data_file(INSTRUMENTS_TABLE, INSTRUMENT, H0)
    before = path.read_bytes()

    wrote = defs.write_hours({INSTRUMENT}, done, STREAMS)

    assert wrote is False
    assert path.read_bytes() == before


def test_each_hour_gets_the_definition_valid_at_its_end(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    change = H0 + HOUR_NS + 20 * 60 * SECOND_NS  # inside the second hour
    defs.read(_file(store, [_btc(H0), _btc(change, max_quantity="5000.000")]), {INSTRUMENT})
    done = store.load_done()
    _finished(done, H0, H0 + HOUR_NS)

    defs.write_hours({INSTRUMENT}, done, STREAMS)

    assert str(_row(store, H0)[1].max_quantity) != "5000.000"
    assert str(_row(store, H0 + HOUR_NS)[1].max_quantity) == "5000.000"


def test_an_hour_without_any_definition_is_skipped_and_logged_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    done = store.load_done()
    _finished(done, H0)

    first = defs.write_hours({INSTRUMENT}, done, STREAMS)
    _finished(done, H0 + HOUR_NS)
    second = defs.write_hours({INSTRUMENT}, done, STREAMS)

    assert (first, second) == (False, False)
    assert OURS not in done.hours
    assert not store.data_file(INSTRUMENTS_TABLE, INSTRUMENT, H0).exists()
    assert _missing(caplog) == [{"code": "instrument_definition_missing", "instrument_id": INSTRUMENT}]


def test_a_file_never_read_is_not_deletable(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    file = _file(store, [_btc(H0)])

    assert defs.deletable(file, {INSTRUMENT}, store.load_done()) is False


def test_a_read_file_is_deletable_once_every_instrument_has_a_row_at_or_after_its_newest_hour(tmp_path):
    store = CatalogStore(tmp_path)
    defs = InstrumentDefinitions(store)
    newest = H0 + HOUR_NS + 5 * SECOND_NS
    file = _file(store, [_btc(H0), _eth(H0), _btc(newest, max_quantity="5000.000")])
    both = {INSTRUMENT, ETH}
    defs.read(file, both)
    done = store.load_done()

    _finished(done, H0, table=INSTRUMENTS_TABLE)
    _finished(done, H0, table=INSTRUMENTS_TABLE, instrument=ETH)
    assert defs.deletable(file, both, done) is False  # neither has the newest row's hour yet

    _finished(done, H0 + HOUR_NS, table=INSTRUMENTS_TABLE)
    assert defs.deletable(file, both, done) is False  # ETH still lacks it

    _finished(done, H0 + HOUR_NS, table=INSTRUMENTS_TABLE, instrument=ETH)
    assert defs.deletable(file, both, done) is True
