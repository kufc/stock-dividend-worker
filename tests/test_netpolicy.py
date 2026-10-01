"""模型已經下載時完全不連網；第一次使用才連網下載。"""

import os
import sys
import types

import pytest

from media_sorter import netpolicy
from media_sorter.config import MODEL_PRESETS


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")


def cache_preset(cache, preset):
    info = MODEL_PRESETS[preset]
    touch(cache / netpolicy.cache_folder(info["hf_repo"]) / "snapshots" / "a" / "open_clip_model.safetensors")
    touch(cache / netpolicy.cache_folder(info["tokenizer_repo"]) / "snapshots" / "b" / "tokenizer.json")


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    yield
    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:
        constants.HF_HUB_OFFLINE = False


def test_telemetry_is_always_disabled():
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_offline_only_when_model_and_tokenizer_are_both_cached(tmp_path):
    assert netpolicy.prepare_for("standard", tmp_path) is False
    assert os.environ["HF_HUB_OFFLINE"] == "0"  # 第一次：允許連網下載
    info = MODEL_PRESETS["standard"]
    touch(tmp_path / netpolicy.cache_folder(info["hf_repo"]) / "snapshots" / "a" / "m.bin")
    assert netpolicy.prepare_for("standard", tmp_path) is False  # 斷詞器還沒有
    cache_preset(tmp_path, "standard")
    assert netpolicy.prepare_for("standard", tmp_path) is True
    assert os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert netpolicy.prepare_for("accurate", tmp_path) is False  # 換成還沒下載的模型就要連網


def test_empty_snapshot_folder_is_not_a_download(tmp_path):
    info = MODEL_PRESETS["standard"]
    (tmp_path / netpolicy.cache_folder(info["hf_repo"]) / "snapshots" / "a").mkdir(parents=True)
    assert not netpolicy.repo_cached(info["hf_repo"], tmp_path)


def test_switching_also_updates_an_already_loaded_huggingface_hub(monkeypatch):
    fake = types.SimpleNamespace(HF_HUB_OFFLINE=False)
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", fake)
    netpolicy.set_offline(True)
    assert fake.HF_HUB_OFFLINE is True
    netpolicy.set_offline(False)
    assert fake.HF_HUB_OFFLINE is False


def test_cache_location_follows_huggingface_rules():
    assert netpolicy.hf_cache_dir({"HF_HUB_CACHE": "/a"}).as_posix() == "/a"
    assert netpolicy.hf_cache_dir({"HF_HOME": "/b"}).as_posix() == "/b/hub"


def test_repo_names_match_what_open_clip_downloads():
    open_clip = pytest.importorskip("open_clip")
    import json
    from pathlib import Path

    for preset in MODEL_PRESETS.values():
        cfg = open_clip.get_pretrained_cfg(preset["arch"], preset["pretrained"])
        assert cfg["hf_hub"].strip("/") == preset["hf_repo"]
        model_cfg = json.loads((Path(open_clip.__file__).parent / "model_configs" / f"{preset['arch']}.json")
                               .read_text())
        assert model_cfg["text_cfg"]["hf_tokenizer_name"] == preset["tokenizer_repo"]


def test_uninstaller_removes_the_same_models():
    from media_sorter import uninstaller

    assert set(uninstaller.MODEL_CACHE_DIRS) == {netpolicy.cache_folder(p["hf_repo"]) for p in MODEL_PRESETS.values()}


class FakeOpenClip:
    """記錄載入當下是不是離線模式；可以設定成「離線載入失敗」（快取不完整）。"""

    def __init__(self, fail_offline=False):
        self.fail_offline, self.calls = fail_offline, []

    def create_model_and_transforms(self, arch, pretrained, device, precision):
        offline = os.environ["HF_HUB_OFFLINE"] == "1"
        self.calls.append(offline)
        if offline and self.fail_offline:
            raise OSError("offline mode and file is not in cache")
        torch = pytest.importorskip("torch")

        class Model(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.logit_scale = torch.nn.Parameter(torch.tensor(4.6))

        return Model(), None, None

    def get_tokenizer(self, arch):
        return lambda texts: None


@pytest.mark.parametrize("fail_offline,expected_calls", [(False, [True]), (True, [True, False])])
def test_classifier_loads_offline_and_falls_back_to_download(tmp_path, monkeypatch, fail_offline, expected_calls):
    pytest.importorskip("torch")
    from media_sorter import classifier

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    cache_preset(tmp_path, "standard")
    fake = FakeOpenClip(fail_offline)
    monkeypatch.setitem(sys.modules, "open_clip", fake)
    clf = classifier.Classifier("standard", device="cpu")
    assert fake.calls == expected_calls  # 已下載：先離線；快取壞掉才改連網補齊
    assert clf.loaded_offline is (not fail_offline)


def test_classifier_downloads_when_not_cached(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    from media_sorter import classifier

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    fake = FakeOpenClip()
    monkeypatch.setitem(sys.modules, "open_clip", fake)
    clf = classifier.Classifier("standard", device="cpu")
    assert fake.calls == [False] and clf.loaded_offline is False
