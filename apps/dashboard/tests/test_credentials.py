"""covers: spec 0004 AC-19, the credentials command makes usable files and never overwrites one."""

import io
import os
import stat
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
from argon2 import PasswordHasher
from pydantic import SecretStr

from dashboard import credentials
from dashboard.credentials import CredentialsError, make_credentials, write_files
from dashboard.settings import DashboardSettings, credentials_guard

CHEAP = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def test_made_credentials_pass_the_dashboard_guard():
    made = make_credentials("long enough password", CHEAP)
    credentials_guard(DashboardSettings(trading_env="demo"), {k: SecretStr(v) for k, v in made.files().items()})
    assert PasswordHasher().verify(made.password_hash, "long enough password")
    assert made.totp_seed == made.totp_seed.upper()
    assert "=" not in made.totp_seed
    assert len(made.totp_seed) == 32
    assert len(made.session_key) >= 48


def test_the_otpauth_uri_carries_the_seed():
    made = make_credentials("long enough password", CHEAP)
    uri = urlsplit(made.otpauth_uri)
    assert uri.scheme == "otpauth"
    assert parse_qs(uri.query)["secret"] == [made.totp_seed]
    parsed = pyotp.parse_uri(made.otpauth_uri)
    assert isinstance(parsed, pyotp.TOTP)
    assert parsed.now() == pyotp.TOTP(made.totp_seed).now()


def test_a_short_password_is_refused():
    with pytest.raises(CredentialsError, match="12 characters"):
        make_credentials("short", CHEAP)


def test_write_creates_three_read_only_files_and_never_overwrites(tmp_path):
    made = make_credentials("long enough password", CHEAP)
    names = ["dashboard_password_hash", "dashboard_totp_seed", "dashboard_session_key"]
    assert write_files(made, tmp_path) == (names, [])
    for name, value in made.files().items():
        assert (tmp_path / name).read_text() == value
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o444

    assert write_files(make_credentials("another long password", CHEAP), tmp_path) == ([], names)
    assert (tmp_path / "dashboard_totp_seed").read_text() == made.totp_seed


def test_an_existing_session_key_from_dev_secrets_is_kept_and_the_others_written(tmp_path, monkeypatch):
    # covers: the laptop flow, dev-secrets.sh makes the session key before the credentials command
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)
    (tmp_path / "dashboard_session_key").write_text("k" * 64)
    out = io.StringIO()
    assert credentials.main(["--dev", "--write", "--out", str(tmp_path)], out=out) == 0
    assert "created  dashboard_password_hash" in out.getvalue()
    assert "created  dashboard_totp_seed" in out.getvalue()
    assert "kept     dashboard_session_key" in out.getvalue()
    assert (tmp_path / "dashboard_session_key").read_text() == "k" * 64


def test_a_kept_seed_warns_that_the_printed_qr_is_not_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)
    (tmp_path / "dashboard_totp_seed").write_text("A" * 32)
    out = io.StringIO()
    credentials.main(["--dev", "--write", "--out", str(tmp_path)], out=out)
    assert "the QR code above is NOT in use" in out.getvalue()


def test_dev_mode_prints_a_random_password_once_and_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)
    out = io.StringIO()
    assert credentials.main(["--dev", "--write", "--out", str(tmp_path)], out=out) == 0
    text = out.getvalue()
    assert text.count("dev password (shown once):") == 1
    assert "otpauth://totp/" in text
    assert "created  dashboard_session_key" in text


def test_the_password_is_asked_twice_and_must_match(monkeypatch, tmp_path):
    answers = iter(["long enough password", "different password!"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    assert credentials.main(["--out", str(tmp_path)], out=io.StringIO()) == 1
    assert list(tmp_path.iterdir()) == []


# --- gaps found against spec 0004 (/test pass)


def test_twelve_characters_is_the_shortest_password_accepted():
    """covers: AC-19"""
    with pytest.raises(CredentialsError):
        make_credentials("x" * 11, CHEAP)
    assert make_credentials("x" * 12, CHEAP).password_hash.startswith("$argon2id$")


def test_a_short_password_through_the_command_exits_1_and_says_why(monkeypatch, capsys, tmp_path):
    """covers: AC-19"""
    monkeypatch.setattr("getpass.getpass", lambda prompt: "short")
    assert credentials.main(["--write", "--out", str(tmp_path)], out=io.StringIO()) == 1
    assert "at least 12 characters" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_two_matching_entries_print_every_value_and_the_qr(monkeypatch, tmp_path):
    """covers: AC-19"""
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)
    prompts = []

    def getpass(prompt):
        prompts.append(prompt)
        return "long enough password"

    monkeypatch.setattr("getpass.getpass", getpass)
    out = io.StringIO()
    assert credentials.main(["--out", str(tmp_path)], out=out) == 0
    text = out.getvalue()
    assert len(prompts) == 2
    assert "dashboard_password_hash: $argon2id$" in text
    seed = next(line for line in text.splitlines() if line.startswith("dashboard_totp_seed: ")).split(": ")[1]
    assert len(seed) == 32
    assert seed == seed.upper()
    assert next(line for line in text.splitlines() if line.startswith("dashboard_session_key: "))
    assert f"secret={seed}" in text
    assert "issuer=Kraken%20Futures%20Scalper" in text
    assert "█" in text or "▀" in text or "▄" in text  # the terminal QR code
    assert "long enough password" not in text  # the typed password is never printed


def test_without_write_nothing_is_written(monkeypatch, tmp_path):
    """covers: AC-19"""
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "long enough password")
    assert credentials.main(["--out", str(tmp_path)], out=io.StringIO()) == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "warning"),
    [
        ("dashboard_password_hash", "the password above does NOT log in"),
        ("dashboard_session_key", "existing sessions stay valid"),
    ],
)
def test_a_kept_hash_or_session_key_says_what_it_means(tmp_path, monkeypatch, name, warning):
    """covers: AC-19"""
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)
    (tmp_path / name).write_text("kept")
    out = io.StringIO()
    credentials.main(["--dev", "--write", "--out", str(tmp_path)], out=out)
    assert f"kept     {name} (already there: {warning}" in out.getvalue()


def test_a_secret_cannot_be_passed_as_an_argument(capsys):
    """covers: AC-19 (nothing secret in process arguments)"""
    with pytest.raises(SystemExit) as info:
        credentials.main(["--password", "long enough password"], out=io.StringIO())
    assert info.value.code == 2


def test_dev_mode_never_asks_and_draws_new_values_each_run(tmp_path, monkeypatch):
    """covers: AC-19"""
    monkeypatch.setattr(credentials, "PasswordHasher", lambda: CHEAP)

    def refuse(prompt):
        raise AssertionError("--dev must not ask for a password")

    monkeypatch.setattr("getpass.getpass", refuse)
    first, second = io.StringIO(), io.StringIO()
    assert credentials.main(["--dev"], out=first) == 0
    assert credentials.main(["--dev"], out=second) == 0

    def values(text):
        return [line for line in text.splitlines() if line.startswith(("dev password", "dashboard_"))]

    assert len(values(first.getvalue())) == 4
    assert set(values(first.getvalue())).isdisjoint(values(second.getvalue()))
