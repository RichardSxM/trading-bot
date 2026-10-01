import pytest
from nautilus_trader.model.identifiers import InstrumentId

from recorder.main import RecorderSettings, build_config


def test_demo_config_records_into_catalog_without_redis(monkeypatch):
    monkeypatch.delenv("RECORDER_ID", raising=False)
    settings = RecorderSettings(trading_env="demo", catalog_path="/tmp/catalog")
    config = build_config(settings)

    assert config.trader_id.value == "RECORDER-DEMO"
    assert config.cache is not None
    assert config.cache is not None
    assert config.cache.database is None
    assert config.streaming is not None
    assert config.streaming.catalog_path == "/tmp/catalog"
    assert config.exec_clients == {}


def test_pinned_instruments_parse(monkeypatch):
    monkeypatch.delenv("RECORDER_INSTRUMENTS", raising=False)
    settings = RecorderSettings(trading_env="demo")
    ids = [InstrumentId.from_str(i) for i in settings.instrument_list]
    assert [i.value for i in ids] == ["PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN"]


def test_data_client_reads_live_public_feeds(monkeypatch):
    from nautilus_trader.adapters.kraken import KrakenEnvironment

    monkeypatch.delenv("RECORDER_ID", raising=False)
    config = build_config(RecorderSettings(trading_env="demo"))
    assert config.data_clients["KRAKEN"].environment == KrakenEnvironment.LIVE


@pytest.mark.parametrize("env", ["demo", "live"])
def test_config_guard_passes_a_recorder_without_execution_clients(monkeypatch, env):
    # The Kraken key variables are refused by boot for every process (core test_env_guard).
    from recorder.main import guard

    monkeypatch.delenv("RECORDER_ID", raising=False)
    guard(RecorderSettings(trading_env=env), {})


def test_live_config_uses_the_live_tag_and_still_no_redis(monkeypatch):
    monkeypatch.delenv("RECORDER_ID", raising=False)
    config = build_config(RecorderSettings(trading_env="live"))

    assert config.trader_id.value == "RECORDER-LIVE"
    assert config.cache is not None
    assert config.cache.database is None
    assert config.exec_clients == {}


def test_explicit_recorder_id_must_match_env(monkeypatch):
    from trading_core.naming import NodeIdMismatch

    monkeypatch.setenv("RECORDER_ID", "RECORDER-DEMO")
    with pytest.raises(NodeIdMismatch):
        build_config(RecorderSettings(trading_env="live"))


def test_catalog_is_flushed_every_second(monkeypatch):
    monkeypatch.delenv("RECORDER_ID", raising=False)
    streaming = build_config(RecorderSettings(trading_env="demo")).streaming
    assert streaming is not None
    assert streaming.flush_interval_ms == 1_000


def test_instrument_list_trims_and_drops_blanks():
    settings = RecorderSettings(trading_env="demo", recorder_instruments=" PF_XBTUSD.KRAKEN, ,PF_SOLUSD.KRAKEN,")

    assert settings.instrument_list == ["PF_XBTUSD.KRAKEN", "PF_SOLUSD.KRAKEN"]


def test_recording_actor_accepts_the_pinned_instruments():
    from recorder.actor import RecordingActor, RecordingConfig

    settings = RecorderSettings(trading_env="demo", recorder_instruments="PF_XBTUSD.KRAKEN,PF_ETHUSD.KRAKEN")

    RecordingActor(RecordingConfig(instrument_ids=settings.instrument_list))


def test_recording_actor_rejects_a_malformed_instrument_id():
    from recorder.actor import RecordingActor, RecordingConfig

    with pytest.raises(ValueError, match=r"missing '\.' separator"):
        RecordingActor(RecordingConfig(instrument_ids=["PF_XBTUSD"]))
