"""covers: spec 0015 AC-26 (``gate check-version``: pass, missing, a stored non pass, bad input, live env)."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from gate.check_version import CheckVersionRefused, check_version
from gate.main import EXIT_NOT_PROMOTED, EXIT_OK, EXIT_REFUSED, _check_version
from gate.settings import GateSettings
from trading_core.recording import repository
from trading_core.recording.enums import DecidedBy, Stage, StrategyKind, Verdict
from trading_core.recording.rows import PromotionRow
from trading_core.versioning import FrozenHashes

NOW = datetime(2026, 10, 21, 12, tzinfo=UTC)


def _version_json(**overrides: object) -> dict[str, object]:
    """``trader.version`` output for a fresh strategy, so tests never share a frozen hash."""
    tag = uuid4().hex
    printed: dict[str, object] = {
        "strategy": f"check_{tag[:8]}",
        "kind": "scalping",
        "config": {"interval_s": 30, "tag": tag},
        "code_hash": f"code-{tag}",
        "frozen_hash": f"frozen-{tag}",
        "git_commit": "a" * 40,
    }
    printed.update(overrides)
    return printed


def _promote(session: Session, version_id, frozen_hash: str, verdict: Verdict, decided_by: DecidedBy) -> None:
    repository.record_promotion(
        session,
        PromotionRow(
            strategy_version_id=version_id,
            from_stage=Stage.CANDIDATE,
            to_stage=Stage.PAPER,
            verdict=verdict,
            thresholds={},
            decided_by=decided_by,
            notes="check-version test",
            decided_at=NOW,
            frozen_hash=frozen_hash,
        ),
        [],
    )


@pytest.mark.postgres
def test_a_version_without_a_promotion_is_missing_and_its_row_stays(gate_engine):
    printed = _version_json()
    with Session(gate_engine) as session:
        checked = check_version(session, json.dumps(printed), "demo")
        session.commit()
        assert (checked.verdict, checked.passed) == ("missing", False)
        assert checked.frozen_hash == printed["frozen_hash"]
        # The same row the trader records at start: same name, kind, commit, config, and hashes.
        same = repository.ensure_strategy_version(
            session,
            str(printed["strategy"]),
            StrategyKind.SCALPING,
            str(printed["git_commit"]),
            printed["config"],  # type: ignore[arg-type]  # a JSON object built above
            FrozenHashes(str(printed["code_hash"]), str(printed["frozen_hash"])),
        )
        assert same == checked.version_id
        assert checked.to_json() == {
            "version_id": str(checked.version_id),
            "frozen_hash": printed["frozen_hash"],
            "stage_needed": "paper",
            "verdict": "missing",
        }


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("verdict", "decided_by", "expected"),
    [
        (Verdict.PASS, DecidedBy.MANUAL, "pass"),
        (Verdict.FAIL, DecidedBy.GATE, "fail"),
        (Verdict.INSUFFICIENT, DecidedBy.GATE, "insufficient"),
    ],
)
def test_the_verdict_is_the_latest_paper_promotion_of_the_frozen_hash(gate_engine, verdict, decided_by, expected):
    printed = _version_json()
    text = json.dumps(printed)
    with Session(gate_engine) as session:
        first = check_version(session, text, "demo")
        _promote(session, first.version_id, first.frozen_hash, verdict, decided_by)
        session.commit()
        again = check_version(session, text, "demo")
    assert (again.version_id, again.verdict, again.passed) == (first.version_id, expected, expected == "pass")


@pytest.mark.postgres
def test_a_later_non_pass_overrides_an_earlier_pass(gate_engine):
    text = json.dumps(_version_json())
    with Session(gate_engine) as session:
        first = check_version(session, text, "demo")
        _promote(session, first.version_id, first.frozen_hash, Verdict.PASS, DecidedBy.MANUAL)
        session.commit()
        repository.record_promotion(
            session,
            PromotionRow(
                strategy_version_id=first.version_id,
                from_stage=Stage.CANDIDATE,
                to_stage=Stage.PAPER,
                verdict=Verdict.FAIL,
                thresholds={},
                decided_by=DecidedBy.GATE,
                decided_at=datetime(2026, 10, 21, 13, tzinfo=UTC),
                frozen_hash=first.frozen_hash,
            ),
            [],
        )
        session.commit()
        assert check_version(session, text, "demo").verdict == "fail"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not JSON"),
        ("[1, 2]", "not a JSON object"),
        (json.dumps(_version_json(git_commit=None)), "no git_commit"),
        (json.dumps(_version_json(git_commit="unknown")), "unknown"),
        (json.dumps(_version_json(frozen_hash="")), "no frozen_hash"),
        (json.dumps({k: v for k, v in _version_json().items() if k != "code_hash"}), "no code_hash"),
        (json.dumps(_version_json(kind="weekly")), "not a strategy kind"),
        (json.dumps(_version_json(config="x")), "no config object"),
    ],
)
def test_bad_input_is_refused_before_any_write(text, message):
    # No session is touched: the parse comes before the version is recorded.
    with pytest.raises(CheckVersionRefused, match=message):
        check_version(None, text, "demo")  # type: ignore[arg-type]  # never reached


def test_a_live_env_is_refused_until_feature_19():
    with pytest.raises(CheckVersionRefused, match="feature 19"):
        check_version(None, json.dumps(_version_json()), "live")  # type: ignore[arg-type]  # never reached


@pytest.mark.postgres
def test_the_command_exits_0_on_pass_3_without_one_and_1_on_bad_input(gate_engine, capsys):
    settings = GateSettings(trading_env="demo", postgres_user="gate_app")
    url = gate_engine.url.render_as_string(hide_password=False)
    text = json.dumps(_version_json())

    assert _check_version(settings, url, text) == EXIT_NOT_PROMOTED
    printed = json.loads(capsys.readouterr().out)
    assert printed["verdict"] == "missing"

    with Session(gate_engine) as session:
        _promote(session, printed["version_id"], printed["frozen_hash"], Verdict.PASS, DecidedBy.MANUAL)
        session.commit()
    assert _check_version(settings, url, text) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"

    assert _check_version(settings, url, "{") == EXIT_REFUSED
    out = capsys.readouterr()
    assert out.out == ""
    assert "not JSON" in out.err
