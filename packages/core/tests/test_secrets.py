"""covers: spec 0003 AC-2 (refusals), AC-3 (fingerprint line), AC-5 (redaction), AC-8 (key check), AC-9 (key age)."""

import asyncio
import hashlib
import json
import logging
import os
from datetime import date
from urllib.parse import quote

import pytest

from trading_core.logs import configure_logging
from trading_core.secrets import (
    SecretUnavailable,
    fingerprint,
    key_check_state,
    key_rotation_age,
    load_required,
    read_secret,
    redact,
    watch_key_check,
)

VALUE = "a3f9c1d2e4b5a6978877665544332211"


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_reads_the_value_trimmed_of_its_trailing_newline(tmp_path):
    (tmp_path / "s").write_text(VALUE + "\n")

    assert read_secret("s", tmp_path).get_secret_value() == VALUE


def test_the_returned_secret_masks_itself_when_printed(tmp_path):
    (tmp_path / "s").write_text(VALUE)

    secret = read_secret("s", tmp_path)

    assert VALUE not in repr(secret)
    assert VALUE not in str(secret)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "missing"),
        ("", "empty"),
        ("  \n", "empty"),
        ("xq7", "too_short"),
        ("aaaaaaaa bbbbbbbb", "whitespace"),
        ("aaaaaaaa\tbbbbbbbb", "whitespace"),
    ],
)
def test_an_unusable_file_raises_with_its_reason_and_never_the_value(tmp_path, content, reason):
    if content is not None:
        (tmp_path / "s").write_text(content)

    with pytest.raises(SecretUnavailable) as exc:
        read_secret("s", tmp_path)

    assert (exc.value.name, exc.value.reason) == ("s", reason)
    if content and content.strip():
        assert content.strip() not in str(exc.value)


def test_a_directory_is_not_a_secret(tmp_path):
    (tmp_path / "s").mkdir()

    with pytest.raises(SecretUnavailable) as exc:
        read_secret("s", tmp_path)

    assert exc.value.reason == "not_a_file"


def test_an_unreadable_file_is_refused(tmp_path):
    (tmp_path / "s").write_bytes(b"\xff\xfe" * 10)  # not UTF-8

    with pytest.raises(SecretUnavailable) as exc:
        read_secret("s", tmp_path)

    assert exc.value.reason == "unreadable"


def test_each_load_logs_one_line_with_name_and_fingerprint(tmp_path, capsys):
    configure_logging(process="trader", env="demo")
    (tmp_path / "s").write_text(VALUE)

    read_secret("s", tmp_path)

    (line,) = _lines(capsys)
    assert (line["msg"], line["secret"], line["fp"]) == ("secret loaded", "s", fingerprint(VALUE))
    assert VALUE not in json.dumps(line)


def test_fingerprint_is_the_first_8_hex_of_sha256():
    assert fingerprint(VALUE) == hashlib.sha256(VALUE.encode()).hexdigest()[:8]


def test_load_required_reads_every_name_and_stops_at_the_first_broken_one(tmp_path):
    (tmp_path / "a").write_text(VALUE)

    assert set(load_required(["a"], tmp_path)) == {"a"}
    with pytest.raises(SecretUnavailable) as exc:
        load_required(["a", "b"], tmp_path)
    assert exc.value.name == "b"


def test_redact_replaces_raw_and_url_quoted_forms(tmp_path):
    special = "p:w/d@#xxxxxxxxxxxxxxx"
    (tmp_path / "pw").write_text(special)
    read_secret("pw", tmp_path)

    url = f"postgresql://u:{quote(special, safe='')}@h/d"

    assert redact(f"raw {special}") == "raw [REDACTED:pw]"
    assert redact(url) == "postgresql://u:[REDACTED:pw]@h/d"


def test_nothing_is_redacted_before_a_secret_is_loaded():
    assert redact(f"value {VALUE}") == f"value {VALUE}"


def test_log_lines_redact_message_fields_and_exception_text(tmp_path, capsys):
    configure_logging(process="trader", env="demo")
    (tmp_path / "kraken_api_secret").write_text(VALUE)
    read_secret("kraken_api_secret", tmp_path)
    capsys.readouterr()
    logger = logging.getLogger("t")

    logger.info("msg %s", VALUE)
    logger.info("field", extra={"fields": {"oops": VALUE}})
    try:
        raise RuntimeError(f"auth failed for {VALUE}")
    except RuntimeError:
        logger.exception("exc")

    out = capsys.readouterr().out
    assert VALUE not in out
    assert out.count("[REDACTED:kraken_api_secret]") == 3


def test_key_rotation_age_counts_days_and_refuses_the_future():
    assert key_rotation_age(date(2026, 7, 1), date(2026, 10, 1)) == 92
    assert key_rotation_age(date(2026, 10, 1), date(2026, 10, 1)) == 0
    with pytest.raises(ValueError, match="future"):
        key_rotation_age(date(2026, 10, 2), date(2026, 10, 1))


@pytest.mark.parametrize(
    ("confirmed", "elapsed", "expected"),
    [(True, 0, "confirmed"), (True, 99, "confirmed"), (False, 1, "waiting"), (False, 2, "failed")],
)
def test_key_check_state(confirmed, elapsed, expected):
    assert key_check_state(confirmed, elapsed, 2.0) == expected


class _FakeTime:
    """A monotonic clock the watcher's sleeps move forward, so no real time passes."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _watch(confirmed_after, timeout_s=3.0):
    clock = _FakeTime()
    stops = []
    polls = []

    def is_confirmed():
        polls.append(clock.t)
        return confirmed_after is not None and clock.t - 100.0 >= confirmed_after

    async def run():
        task = asyncio.create_task(
            watch_key_check(is_confirmed, lambda: stops.append(True), timeout_s, poll_s=0, now=clock)
        )
        while not task.done():
            clock.t += 1.0
            await asyncio.sleep(0)
        return task.result()

    return asyncio.run(run()), stops, polls


def test_watcher_fails_closed_after_the_timeout_and_stops_the_node(capsys):
    configure_logging(process="trader", env="live")

    state, stops, _ = _watch(confirmed_after=None)

    assert (state, stops) == ("failed", [True])
    (line,) = [line for line in _lines(capsys) if line.get("code") == "kraken_auth_failed"]
    assert line["level"] == "CRITICAL"


def test_watcher_confirms_once_the_account_appears_and_never_stops_the_node(capsys):
    configure_logging(process="trader", env="live")

    state, stops, _ = _watch(confirmed_after=2.0)

    assert (state, stops) == ("confirmed", [])
    assert "kraken account confirmed" in [line["msg"] for line in _lines(capsys)]


def _random(length=32):
    return os.urandom(length).hex()[:length]


@pytest.mark.parametrize(("length", "loads"), [(16, True), (15, False)])
def test_sixteen_characters_is_the_shortest_value_accepted(tmp_path, length, loads):
    (tmp_path / "s").write_text(_random(length))

    if loads:
        assert len(read_secret("s", tmp_path).get_secret_value()) == 16
    else:
        with pytest.raises(SecretUnavailable) as exc:
            read_secret("s", tmp_path)
        assert exc.value.reason == "too_short"


def test_surrounding_whitespace_is_trimmed_and_the_fingerprint_is_of_the_trimmed_value(tmp_path, capsys):
    # covers: AC-3, the fp you compare with the password manager entry on rotation.
    configure_logging(process="trader", env="demo")
    value = _random()
    (tmp_path / "s").write_text(f"  {value} \n")

    secret = read_secret("s", tmp_path)

    (line,) = _lines(capsys)
    assert secret.get_secret_value() == value
    assert line["fp"] == fingerprint(value)


def test_a_value_with_quotes_and_backslashes_is_redacted_in_its_json_escaped_form(tmp_path, capsys):
    # covers: AC-5. A field is JSON encoded before redaction, so '"' and '\' arrive escaped.
    configure_logging(process="trader", env="demo")
    value = f'{_random(8)}"q\\b{_random(8)}'
    (tmp_path / "pw").write_text(value)
    read_secret("pw", tmp_path)
    capsys.readouterr()
    logger = logging.getLogger("t")

    logger.info("field", extra={"fields": {"oops": value}})
    logger.info("msg %s", value)

    out = capsys.readouterr().out
    assert value not in out
    assert json.dumps(value)[1:-1] not in out
    assert out.count("[REDACTED:pw]") == 2


def test_a_value_that_contains_another_is_redacted_whole(tmp_path):
    # covers: AC-5. Longest form first, so no fragment of the longer value survives.
    short = _random(16)
    long = f"{short}{_random(16)}"
    (tmp_path / "short").write_text(short)
    (tmp_path / "long").write_text(long)
    load_required(["short", "long"], tmp_path)

    assert redact(f"a {long} b {short}") == "a [REDACTED:long] b [REDACTED:short]"
