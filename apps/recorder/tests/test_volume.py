import logging
from types import SimpleNamespace

import pytest

from recorder import main as recorder_main
from recorder.settings import RecorderSettings
from recorder.volume import MARKER, CatalogVolumeMissing, check_catalog_volume


def test_not_required_passes_without_the_marker(tmp_path):
    check_catalog_volume(tmp_path, required=False)


def test_required_passes_with_the_marker(tmp_path):
    (tmp_path / MARKER).touch()
    check_catalog_volume(tmp_path, required=True)


def test_required_raises_without_the_marker(tmp_path):
    with pytest.raises(CatalogVolumeMissing) as info:
        check_catalog_volume(tmp_path, required=True)
    assert info.value.path == tmp_path


def test_a_marker_folder_is_not_a_marker(tmp_path):
    (tmp_path / MARKER).mkdir()
    with pytest.raises(CatalogVolumeMissing):
        check_catalog_volume(tmp_path, required=True)


def test_require_marker_defaults_off(monkeypatch):
    monkeypatch.delenv("CATALOG_REQUIRE_MARKER", raising=False)
    assert RecorderSettings(trading_env="demo").catalog_require_marker is False


def test_require_marker_reads_the_env(monkeypatch):
    monkeypatch.setenv("CATALOG_REQUIRE_MARKER", "true")
    assert RecorderSettings(trading_env="demo").catalog_require_marker is True


def test_main_refuses_before_building_the_node(monkeypatch, tmp_path, caplog):
    settings = RecorderSettings(trading_env="demo", catalog_path=str(tmp_path), catalog_require_marker=True)
    monkeypatch.setattr(recorder_main, "boot", lambda *a, **k: SimpleNamespace(settings=settings))

    def no_node(*a, **k):
        raise AssertionError("the node must not be built without the catalog disk")

    monkeypatch.setattr(recorder_main, "TradingNode", no_node)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as info:
        recorder_main.main()

    assert info.value.code == 1
    record = next(r for r in caplog.records if r.levelno == logging.CRITICAL)
    assert record.fields == {"code": "catalog_volume_missing", "path": str(tmp_path)}
    assert list(tmp_path.iterdir()) == []
