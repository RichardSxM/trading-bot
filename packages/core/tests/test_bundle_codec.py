"""covers: spec 0015 AC-7 (a bundle file parses back into the exact bundle; old bundles still parse; a
large file is read in parts, never whole) and spec 0022 AC-12 (a bundle carries every coin's instrument)."""

import json
import tracemalloc
from dataclasses import fields, replace
from typing import Any
from uuid import uuid7

import pytest
from bundle_factory import eth_instrument, instrument, study, study_bundle, version

from trading_core.recording.bundle_codec import BundleFile, BundleFormatError, bundle_json, parse_bundle
from trading_core.recording.rows import FillRow, RunBundle


def _as_tuples(bundle: RunBundle) -> dict[str, object]:
    return {f.name: tuple(v) if isinstance(v := getattr(bundle, f.name), list) else v for f in fields(bundle)}


def test_a_full_study_bundle_round_trips():
    strategy_id = uuid7()
    bundle = study_bundle(study(strategy_id, "s1"), version(strategy_id, {"fast": 12}))

    parsed = parse_bundle(bundle_json(bundle))

    assert _as_tuples(parsed) == _as_tuples(bundle)
    assert bundle_json(parsed) == bundle_json(bundle)


def test_a_bundle_from_before_spec_0015_still_parses():
    strategy_id = uuid7()
    data = json.loads(bundle_json(study_bundle(study(strategy_id, "s1"), version(strategy_id, {}))))
    for key in ("study", "study_run", "daily_equity", "measures", "version", "instrument", "strategy_name"):
        data.pop(key)

    parsed = parse_bundle(json.dumps(data))

    assert parsed.study is None
    assert parsed.daily_equity == ()


def test_every_coins_instrument_round_trips_and_the_head_carries_them(tmp_path):
    strategy_id = uuid7()
    bundle = replace(
        study_bundle(study(strategy_id, "s1"), version(strategy_id, {})),
        instruments=(eth_instrument(), instrument()),
    )
    text = bundle_json(bundle)

    assert parse_bundle(text).instruments == (eth_instrument(), instrument())
    assert _file(tmp_path, text).head().instruments == (eth_instrument(), instrument())


def test_a_bundle_from_before_spec_0022_parses_with_no_instruments():
    strategy_id = uuid7()
    data = json.loads(bundle_json(study_bundle(study(strategy_id, "s1"), version(strategy_id, {}))))
    data.pop("instruments")

    parsed = parse_bundle(json.dumps(data))

    assert parsed.instruments == ()
    assert parsed.instrument == instrument()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["run"].update(mode="paperish"), "RunMode"),
        (lambda d: d["results"][0].update(fees_total=0.6), "decimal string"),
        (lambda d: d["run"].pop("env"), "env is missing"),
        (lambda d: d["run"].update(started_at="2026-10-19T12:00:00"), "UTC"),
        (lambda d: d["run"].update(id="not-a-uuid"), "run.id: 'not-a-uuid' is not a UUID"),
        (lambda d: d["run"].update(started_at="yesterday"), "is not a datetime"),
        (lambda d: d["daily_equity"][0].update(day="2026-13-40"), "is not a date"),
        (lambda d: d["results"][0].update(fees_total="0.6x"), "is not a decimal"),
    ],
)
def test_a_malformed_bundle_is_refused(change, message, tmp_path):
    strategy_id = uuid7()
    data = json.loads(bundle_json(study_bundle(study(strategy_id, "s1"), version(strategy_id, {}))))
    change(data)

    with pytest.raises(BundleFormatError, match=message):
        parse_bundle(json.dumps(data))
    with pytest.raises(BundleFormatError, match=message):
        _read_in_parts(_file(tmp_path, json.dumps(data)))


def test_text_that_is_not_json_is_refused():
    with pytest.raises(BundleFormatError, match="not JSON"):
        parse_bundle("{")


def _file(tmp_path, text: str) -> BundleFile:
    path = tmp_path / "run.bundle.json"
    path.write_text(text)
    return BundleFile(path)


def _read_in_parts(file: BundleFile) -> RunBundle:
    head = file.head()
    lists: dict[str, Any] = {
        f.name: tuple(file.rows(f.name, type(rows[0])))
        for f in fields(head)
        if isinstance(rows := getattr(BIG, f.name), list | tuple) and rows
    }
    return replace(head, **lists)


BIG = study_bundle(study(uuid7(), "s1"), version(uuid7(), {}))


def test_a_bundle_read_in_parts_is_the_parsed_bundle(tmp_path):
    text = bundle_json(BIG)

    assert _as_tuples(_read_in_parts(_file(tmp_path, text))) == _as_tuples(parse_bundle(text))


def test_the_head_leaves_the_row_lists_empty(tmp_path):
    head = _file(tmp_path, bundle_json(BIG)).head()

    assert head.run == BIG.run
    assert head.study == BIG.study
    assert head.measures == BIG.measures
    assert head.fills == ()
    assert head.daily_equity == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "not JSON"),
        ("[]", "must be an object"),
        (json.dumps({"fills": {"a": 1}}), "bundle.fills must be a list"),
    ],
)
def test_a_malformed_file_is_refused_in_parts(text, message, tmp_path):
    with pytest.raises(BundleFormatError, match=message):
        _file(tmp_path, text).head()


def test_rows_of_the_wrong_class_are_refused(tmp_path):
    with pytest.raises(ValueError, match="does not hold"):
        next(_file(tmp_path, bundle_json(BIG)).rows("orders", FillRow))


def test_a_large_file_is_never_held_whole(tmp_path):
    fill = BIG.fills[0]
    fills = [replace(fill, trade_id=f"T-{i}") for i in range(20_000)]
    file = _file(tmp_path, bundle_json(replace(BIG, fills=fills)))
    size = file.path.stat().st_size

    tracemalloc.start()
    try:
        file.head()
        count = sum(1 for _ in file.rows("fills", FillRow))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert count == 20_000
    assert size > 10_000_000
    assert peak < size / 10  # json.loads alone needs several times the size
