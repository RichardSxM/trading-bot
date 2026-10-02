"""covers: spec 0004 AC-1 to AC-4, the pure login pieces on a fake clock."""

import pyotp
import pytest
from itsdangerous import URLSafeSerializer

from dashboard.auth.lockout import Lockout
from dashboard.auth.next_path import referer_path, safe_next
from dashboard.auth.password import PasswordChecker
from dashboard.auth.proxy import default_gateway
from dashboard.auth.session import SALT, SessionCodec
from dashboard.auth.totp import TotpGuard

from .conftest import PASSWORD, PASSWORD_HASH, SEED, SESSION_KEY

# --- password


def test_password_checker_accepts_only_the_right_password():
    checker = PasswordChecker(PASSWORD_HASH)
    assert checker.verify(PASSWORD) is True
    assert checker.verify("wrong") is False
    assert PasswordChecker("not a hash").verify(PASSWORD) is False


# --- TOTP (AC-3)

NOW = 1_790_000_000.0  # a fixed instant, mid step
STEP = int(NOW // 30)


def _code(step):
    return pyotp.TOTP(SEED).at(step * 30)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_a_code_matches_its_step_within_one_step_either_side(offset):
    assert TotpGuard(SEED).matched_step(_code(STEP + offset), NOW) == STEP + offset


@pytest.mark.parametrize("offset", [-2, 2])
def test_a_code_two_steps_away_does_not_match(offset):
    assert TotpGuard(SEED).matched_step(_code(STEP + offset), NOW) is None


def test_garbage_does_not_match():
    assert TotpGuard(SEED).matched_step("12345", NOW) is None
    assert TotpGuard(SEED).matched_step("", NOW) is None


def test_the_accepted_step_and_older_ones_are_replays():
    guard = TotpGuard(SEED)
    assert guard.is_replay(STEP) is False
    guard.accept(STEP)
    assert guard.is_replay(STEP) is True
    assert guard.is_replay(STEP - 1) is True
    assert guard.is_replay(STEP + 1) is False


# --- lockout (AC-2)


def test_five_failures_lock_the_key_until_the_window_from_the_first_failure_ends():
    lockout = Lockout()
    for i in range(5):
        assert lockout.is_locked("a", 100 + i) is False
        lockout.record_failure("a", 100 + i)
    assert lockout.is_locked("a", 105) is True
    assert lockout.is_locked("a", 100 + 899) is True
    assert lockout.is_locked("a", 100 + 900) is False
    assert lockout.is_locked("b", 105) is False


def test_four_failures_then_a_success_resets_the_key():
    lockout = Lockout()
    for i in range(4):
        lockout.record_failure("a", i)
    lockout.record_success("a")
    for i in range(4):
        lockout.record_failure("a", 10 + i)
    assert lockout.is_locked("a", 20) is False


def test_twenty_failures_across_keys_lock_every_login():
    lockout = Lockout()
    for i in range(20):
        lockout.record_failure(f"k{i}", 100 + i)
    assert lockout.is_locked("someone-new", 120) is True
    assert lockout.is_locked("someone-new", 100 + 900) is False  # the first failure aged out


def test_at_most_1000_keys_are_held_oldest_evicted():
    lockout = Lockout(max_global_failures=10_000)
    for _ in range(4):
        lockout.record_failure("first", 0)
    for i in range(1000):
        lockout.record_failure(f"k{i}", 1)
    lockout.record_failure("first", 2)  # a fresh window: the four old failures were evicted
    assert lockout.is_locked("first", 3) is False


# --- session (AC-4)

FP = "abcd1234"


def _codec(idle_s=24 * 3600, max_s=7 * 86400, fp=FP, key=SESSION_KEY):
    return SessionCodec(key, fp, idle_s=idle_s, max_s=max_s)


def test_a_session_round_trips_and_carries_a_csrf_token():
    codec = _codec()
    session = codec.new(1000)
    assert codec.decode(codec.encode(session), 1001) == session
    assert len(session.csrf) >= 40


def test_idle_limit_slides_with_each_request_and_the_hard_cap_does_not():
    codec = _codec(idle_s=100, max_s=250)
    session = codec.new(0)
    assert codec.decode(codec.encode(session), 99) is not None
    assert codec.decode(codec.encode(session), 100) is None
    touched = codec.touch(codec.touch(session, 90), 180)
    assert codec.decode(codec.encode(touched), 249) is not None
    assert codec.decode(codec.encode(touched), 250) is None  # 250 s after login, despite activity


def test_tampered_foreign_and_old_password_cookies_are_no_session():
    codec = _codec()
    cookie = codec.encode(codec.new(0))
    assert codec.decode(cookie[:-1] + ("A" if cookie[-1] != "A" else "B"), 1) is None
    assert codec.decode(None, 1) is None
    assert codec.decode("", 1) is None
    assert _codec(key="x" * 48).decode(cookie, 1) is None  # a rotated session key ends it
    assert _codec(fp="ffffffff").decode(cookie, 1) is None  # a new password ends it


@pytest.mark.parametrize(
    "payload",
    [[1], {"iat": True, "seen": 0, "csrf": "c", "pw": FP}, {"iat": 0, "seen": "0", "csrf": "c", "pw": FP}, {"iat": 0}],
)
def test_a_signed_but_malformed_payload_is_no_session(payload):
    cookie = URLSafeSerializer(SESSION_KEY, salt=SALT).dumps(payload)
    assert _codec().decode(cookie, 1) is None


# --- next (AC-1)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/trades", "/trades"),
        ("/a/b_c-d", "/a/b_c-d"),
        ("/", "/"),
        ("/login", "/"),
        ("//evil.example", "/"),
        ("//evil", "/"),  # protocol relative: another host, e.g. a MagicDNS name
        ("/a//b", "/a//b"),
        ("https://evil.example/", "/"),
        ("/x?y=1", "/"),
        ("/%2F", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_next_is_a_plain_local_path_or_root(candidate, expected):
    assert safe_next(candidate) == expected


def test_referer_path_takes_only_the_path():
    assert referer_path("https://kfs.example.ts.net/trades") == "/trades"
    assert referer_path("https://kfs.example.ts.net/login") == "/"
    assert referer_path(None) == "/"


# --- lockout key: the trusted gateway

HEADER = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def test_default_gateway_reads_the_default_route():
    table = HEADER + "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    table += "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    assert default_gateway(table) == "172.18.0.1"


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n",
        "eth0\t00000000\tnothex\t0003\n",
        "eth0\t00000000\t1FFFFFFFF\t0003\n",
        "eth0\t00000000\t00000000\t0003\n",
        "eth0\t00000000\n",
    ],
)
def test_default_gateway_is_none_without_a_usable_default_route(rows):
    assert default_gateway(HEADER + rows) is None
