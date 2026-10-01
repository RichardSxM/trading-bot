"""covers: spec 0001 Redis (env tagged node ids, so demo and live never share keys)."""

import pytest

from trading_core.naming import NodeIdMismatch, env_tag, key_prefix, node_id


@pytest.mark.parametrize(
    ("name", "env", "expected"),
    [("TRADER", "demo", "TRADER-DEMO"), ("trader", "live", "TRADER-LIVE"), ("RECORDER", "live", "RECORDER-LIVE")],
)
def test_node_id_is_derived_from_the_environment(name, env, expected):
    assert node_id(name, env) == expected


@pytest.mark.parametrize("explicit", ["TRADER-DEMO", "trader-demo", "TRADER-Demo"])
def test_explicit_id_with_a_matching_tag_is_kept_as_given(explicit):
    assert node_id("TRADER", "demo", explicit) == explicit


@pytest.mark.parametrize("explicit", ["TRADER-LIVE", "TRADER", "TRADER-", "TRADER-DEMO-2"])
def test_explicit_id_whose_tag_is_not_the_environment_is_refused(explicit):
    with pytest.raises(NodeIdMismatch, match="TRADING_ENV='demo'"):
        node_id("TRADER", "demo", explicit)


def test_env_tag_and_key_prefix():
    assert env_tag("live") == "LIVE"
    assert key_prefix("demo", "trader") == "demo:trader:"
    assert key_prefix("live", "snapshot") == "live:snapshot:"
