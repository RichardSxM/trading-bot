"""covers: spec 0015 AC-7 (a bundle file parses back into the exact bundle; old bundles still parse)."""

import json
from dataclasses import fields
from uuid import uuid7

import pytest
from bundle_factory import study, study_bundle, version

from trading_core.recording.bundle_codec import BundleFormatError, bundle_json, parse_bundle
from trading_core.recording.rows import RunBundle


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


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["run"].update(mode="paperish"), "RunMode"),
        (lambda d: d["results"][0].update(fees_total=0.6), "decimal string"),
        (lambda d: d["run"].pop("env"), "env is missing"),
        (lambda d: d["run"].update(started_at="2026-10-19T12:00:00"), "UTC"),
    ],
)
def test_a_malformed_bundle_is_refused(change, message):
    strategy_id = uuid7()
    data = json.loads(bundle_json(study_bundle(study(strategy_id, "s1"), version(strategy_id, {}))))
    change(data)

    with pytest.raises(BundleFormatError, match=message):
        parse_bundle(json.dumps(data))


def test_text_that_is_not_json_is_refused():
    with pytest.raises(BundleFormatError, match="not JSON"):
        parse_bundle("{")
