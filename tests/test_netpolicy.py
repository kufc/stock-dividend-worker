"""連網政策：辨識時嚴格離線；只有使用者明確同意下載或修復時才連網。

第九輪外部審查（P1）：原本離線載入丟出「任何」例外，程式都會關掉離線模式再試一次，
而且「已下載」只看資料夾裡有沒有檔案。現在是三種明確的狀態（見 netpolicy 的說明），
這裡用真的把 socket 擋掉的方式證明離線模式不會連網。
"""

import hashlib
import os
import socket
import sys
import types

import pytest

from media_sorter import netpolicy
from media_sorter.config import MODEL_PRESETS, WEIGHTS_FILE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


WEIGHTS = b"weights " * 100
FILES = {"config.json": b"{}", "tokenizer.json": b"tok" * 10}


@pytest.fixture
def preset(tmp_path, monkeypatch):
    """一個小小的假模型（真的 standard 模型有 1.5 GB），放在暫存的快取資料夾裡。"""
    info = dict(MODEL_PRESETS["standard"])
    info["hf_files"] = {WEIGHTS_FILE: (len(WEIGHTS), sha(WEIGHTS))}
    info["tokenizer_files"] = {name: (len(data), sha(data)) for name, data in FILES.items()}
    monkeypatch.setitem(MODEL_PRESETS, "standard", info)
    cache = tmp_path / "hub"
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(netpolicy, "MODEL_STAMP_FILE", tmp_path / "stamps.json")
    return types.SimpleNamespace(cache=cache, info=info, stamps=tmp_path / "stamps.json")


def write_model(preset, weights=WEIGHTS, files=FILES):
    for file in netpolicy.preset_files("standard"):
        data = weights if file.name == WEIGHTS_FILE else files.get(file.name)
        if data is None:
            continue
        path = file.path(preset.cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.fixture(autouse=True)
def _restore_offline():
    yield
    netpolicy.set_offline(True)


@pytest.fixture
def no_network(monkeypatch):
    """把所有對外連線擋掉並記錄下來：任何嘗試都會失敗，而且測試看得到。"""
    attempts = []

    def blocked(name):
        def refuse(*args, **kwargs):
            attempts.append((name, args[:2]))
            raise OSError(f"network blocked by test: {name}")
        return refuse

    monkeypatch.setattr(socket.socket, "connect", blocked("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", blocked("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("getaddrinfo"))
    return attempts


# ---------------------------------------------------------------------------- 基本狀態
def test_telemetry_is_always_disabled_and_offline_is_the_default():
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["TRANSFORMERS_OFFLINE"] == "1"
    assert netpolicy.is_offline()


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


def test_every_preset_pins_a_commit_and_lists_hashes():
    for name, info in MODEL_PRESETS.items():
        for kind in ("hf", "tokenizer"):
            assert len(info[f"{kind}_revision"]) == 40 and int(info[f"{kind}_revision"], 16) >= 0, name
            for size, digest in info[f"{kind}_files"].values():
                assert size > 0 and len(digest) == 64
        assert WEIGHTS_FILE in info["hf_files"]
        assert {"config.json", "tokenizer.json", "sentencepiece.bpe.model"} <= set(info["tokenizer_files"])


# ---------------------------------------------------------------------------- 完整性：不是「資料夾裡有檔案」就算
def test_having_some_files_in_the_folder_is_not_enough(preset):
    """舊的判斷：snapshots 底下任何一個版本有任何檔案就算已下載。"""
    for repo in (preset.info["hf_repo"], preset.info["tokenizer_repo"]):
        other = preset.cache / netpolicy.cache_folder(repo) / "snapshots" / "some-other-revision" / "x.bin"
        other.parent.mkdir(parents=True)
        other.write_text("x")
    assert not netpolicy.preset_cached("standard", preset.cache)
    assert len(netpolicy.check_preset("standard", preset.cache)) == 3


def test_complete_model_passes_and_each_kind_of_damage_is_reported(preset):
    write_model(preset)
    assert netpolicy.check_preset("standard", preset.cache) == []
    assert netpolicy.preset_cached("standard", preset.cache) and netpolicy.prepare_for("standard", preset.cache)
    weights = netpolicy.preset_files("standard")[0].path(preset.cache)

    weights.write_bytes(WEIGHTS[:-10])  # 下載中斷
    assert "不完整" in netpolicy.check_preset("standard", preset.cache)[0]
    assert not netpolicy.preset_cached("standard", preset.cache)

    weights.write_bytes(b"X" + WEIGHTS[1:])  # 大小一樣、內容不對
    assert netpolicy.preset_cached("standard", preset.cache)  # 只看大小的快速檢查看不出來⋯
    assert "內容不正確" in netpolicy.check_preset("standard", preset.cache)[0]  # ⋯載入前的核對看得出來
    assert not netpolicy.prepare_for("standard", preset.cache)

    weights.unlink()
    assert "缺少" in netpolicy.check_preset("standard", preset.cache)[0]


def test_hash_is_remembered_until_the_file_changes(preset, monkeypatch):
    write_model(preset)
    assert netpolicy.check_preset("standard", preset.cache) == []
    real = netpolicy.sha256_of
    monkeypatch.setattr(netpolicy, "sha256_of", lambda *a, **k: pytest.fail("不應該重算雜湊"))
    assert netpolicy.check_preset("standard", preset.cache) == []  # 核對過、檔案沒變：不用重算
    monkeypatch.setattr(netpolicy, "sha256_of", real)
    weights = netpolicy.preset_files("standard")[0].path(preset.cache)
    st = weights.stat()
    weights.write_bytes(b"X" + WEIGHTS[1:])
    os.utime(weights, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert netpolicy.check_preset("standard", preset.cache)  # 檔案被動過：重算並發現
    assert netpolicy.check_preset("standard", preset.cache, deep=True)


# ---------------------------------------------------------------------------- 下載與修復：只在明確呼叫時連網
def test_download_only_goes_online_inside_the_call_and_verifies(preset):
    seen = []

    def fetch(file):
        seen.append((file.name, file.revision, netpolicy.is_offline()))
        path = file.path(preset.cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(WEIGHTS if file.name == WEIGHTS_FILE else FILES[file.name])

    done = netpolicy.download_preset("standard", preset.cache, fetch=fetch)
    assert sorted(done) == sorted([WEIGHTS_FILE, *FILES])
    assert all(offline is False for _, _, offline in seen)  # 下載期間允許連網
    assert {rev for _, rev, _ in seen} == {preset.info["hf_revision"], preset.info["tokenizer_revision"]}  # 固定版本
    assert netpolicy.is_offline()  # 結束後回到離線
    assert netpolicy.download_preset("standard", preset.cache, fetch=lambda f: pytest.fail("不需要再下載")) == []


def test_failed_or_wrong_download_returns_to_offline_and_is_rejected(preset):
    with pytest.raises(ConnectionError):
        netpolicy.download_preset("standard", preset.cache,
                                  fetch=lambda f: (_ for _ in ()).throw(ConnectionError("斷線")))
    assert netpolicy.is_offline()

    def wrong(file):  # 下載到的內容跟固定的雜湊不符（上游被換掉、中途被竄改）
        path = file.path(preset.cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = WEIGHTS if file.name == WEIGHTS_FILE else FILES[file.name]
        path.write_bytes(b"?" + data[1:])

    with pytest.raises(netpolicy.ModelNotReady, match="下載後核對失敗"):
        netpolicy.download_preset("standard", preset.cache, fetch=wrong)
    assert netpolicy.is_offline() and not netpolicy.prepare_for("standard", preset.cache)


def test_repair_rehashes_and_refetches_only_the_bad_file(preset):
    write_model(preset)
    assert netpolicy.repair_preset("standard", preset.cache, fetch=lambda f: pytest.fail("沒壞不用下載")) == ([], [])
    weights = netpolicy.preset_files("standard")[0].path(preset.cache)
    netpolicy.check_preset("standard", preset.cache)  # 留下「核對過」的紀錄
    st = weights.stat()
    weights.write_bytes(b"X" + WEIGHTS[1:])  # 之後檔案壞了，但大小、修改時間都沒變（紀錄會以為它沒問題）
    os.utime(weights, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert netpolicy.check_preset("standard", preset.cache) == []
    problems, fetched = netpolicy.repair_preset("standard", preset.cache, allow_download=False)
    assert len(problems) == 1 and fetched == [] and weights.exists()  # 只檢查：不刪、不連網

    def fetch(file):
        assert not file.path(preset.cache).exists()  # 壞的已經先刪掉
        file.path(preset.cache).write_bytes(WEIGHTS)

    problems, fetched = netpolicy.repair_preset("standard", preset.cache, fetch=fetch)
    assert len(problems) == 1 and fetched == [WEIGHTS_FILE]
    assert netpolicy.check_preset("standard", preset.cache, deep=True) == [] and netpolicy.is_offline()


# ---------------------------------------------------------------------------- Classifier：任何錯誤都不連網
class FakeOpenClip:
    """假的 open_clip：記錄每次載入時是不是離線；不是離線就會像真的元件一樣去連 huggingface.co。"""

    def __init__(self, error=None):
        self.error, self.calls, self.configs = error, [], {}

    def get_model_config(self, arch):
        return {"embed_dim": 512, "vision_cfg": {}, "text_cfg": {"hf_model_name": "xlm-roberta-base",
                                                               "hf_tokenizer_name": "xlm-roberta-base"}}

    def add_model_config(self, folder):
        import json
        from pathlib import Path

        for path in Path(folder).glob("*.json"):
            self.configs[path.stem] = json.loads(path.read_text(encoding="utf-8"))

    def create_model_and_transforms(self, arch, pretrained, device, precision):
        offline = os.environ["HF_HUB_OFFLINE"] == "1" and os.environ["TRANSFORMERS_OFFLINE"] == "1"
        self.calls.append((arch, pretrained, offline))
        if not offline:
            socket.create_connection(("huggingface.co", 443), timeout=1)
        if self.error is not None:
            raise self.error
        torch = pytest.importorskip("torch")

        class Model(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.logit_scale = torch.nn.Parameter(torch.tensor(4.6))

        return Model(), None, None

    def get_tokenizer(self, arch):
        return lambda texts: None


@pytest.fixture
def fake_clip(monkeypatch, tmp_path):
    pytest.importorskip("torch")
    from media_sorter import classifier

    monkeypatch.setattr(classifier, "MODEL_CONFIG_DIR", tmp_path / "model-config")

    def install(error=None):
        fake = FakeOpenClip(error)
        monkeypatch.setitem(sys.modules, "open_clip", fake)
        return fake, classifier

    return install


def test_classifier_loads_the_verified_local_files_offline(preset, fake_clip, no_network):
    write_model(preset)
    fake, classifier = fake_clip()
    clf = classifier.Classifier("standard", device="cpu")
    (arch, weights, offline), = fake.calls
    assert offline and clf.loaded_offline and not no_network
    # 交給 open_clip 的是固定版本的本機路徑，不是會被拿去「查最新版本」的名稱
    assert weights == str(netpolicy.snapshot_dir(preset.info["hf_repo"], preset.info["hf_revision"]) / WEIGHTS_FILE)
    tokenizer_dir = str(netpolicy.snapshot_dir(preset.info["tokenizer_repo"], preset.info["tokenizer_revision"]))
    assert fake.configs[arch]["text_cfg"] == {"hf_model_name": tokenizer_dir, "hf_tokenizer_name": tokenizer_dir}


def test_classifier_without_a_model_reports_and_never_connects(preset, fake_clip, no_network):
    fake, classifier = fake_clip()
    with pytest.raises(netpolicy.ModelNotReady) as info:
        classifier.Classifier("standard", device="cpu")
    assert fake.calls == [] and not no_network and netpolicy.is_offline()
    assert info.value.download_bytes == len(WEIGHTS) + sum(len(d) for d in FILES.values())
    assert "不會自行連網" in str(info.value)


@pytest.mark.parametrize("error", [
    OSError("offline mode and file is not in cache"),      # 以前就是這種錯誤觸發「改連網再試一次」
    RuntimeError("CUDA out of memory"), MemoryError(), PermissionError(13, "拒絕存取"),
    ImportError("version mismatch"), ValueError("unexpected key in state_dict"),
])
def test_any_load_error_stays_offline_and_is_not_retried(preset, fake_clip, no_network, error):
    write_model(preset)
    fake, classifier = fake_clip(error)
    with pytest.raises(classifier.ModelLoadError, match="沒有連網重試"):
        classifier.Classifier("standard", device="cpu")
    assert len(fake.calls) == 1 and fake.calls[0][2] is True  # 只試一次，而且是離線
    assert not no_network and netpolicy.is_offline()


def test_damaged_model_is_not_loaded(preset, fake_clip, no_network):
    write_model(preset, weights=b"X" + WEIGHTS[1:])
    fake, classifier = fake_clip()
    with pytest.raises(netpolicy.ModelNotReady, match="內容不正確"):
        classifier.Classifier("standard", device="cpu")
    assert fake.calls == [] and not no_network


# ---------------------------------------------------------------------------- 跟真的套件、真的模型核對
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
        assert model_cfg["text_cfg"]["hf_model_name"] == preset["tokenizer_repo"]
        assert not model_cfg["text_cfg"].get("hf_model_pretrained")  # 文字模型的權重在 CLIP 的檔案裡，不另外下載


def test_uninstaller_removes_the_same_models():
    from media_sorter import uninstaller

    assert set(uninstaller.MODEL_CACHE_DIRS) == {netpolicy.cache_folder(p["hf_repo"]) for p in MODEL_PRESETS.values()}


def _real_model_or_skip():
    pytest.importorskip("torch")
    pytest.importorskip("open_clip")
    if netpolicy.quick_missing("standard"):
        pytest.skip("這台電腦沒有下載真的 standard 模型")


def test_real_model_loads_and_runs_with_the_network_blocked(no_network, tmp_path, monkeypatch):
    """真的 torch、open_clip、transformers 與真的模型檔：把 socket 全部擋掉，載入與辨識照常完成，而且沒有任何連線嘗試。"""
    _real_model_or_skip()
    from media_sorter import classifier

    monkeypatch.setattr(classifier, "MODEL_CONFIG_DIR", tmp_path / "model-config")
    monkeypatch.setattr(netpolicy, "MODEL_STAMP_FILE", tmp_path / "stamps.json")
    clf = classifier.self_test("standard")
    vectors = clf.encode_texts(["一隻貓", "a photo of a dog"])
    assert vectors.shape[0] == 2 and clf.loaded_offline
    assert no_network == [], f"離線模式下有連線嘗試：{no_network}"


def test_real_libraries_with_an_incomplete_cache_report_instead_of_connecting(no_network, tmp_path, monkeypatch):
    """真的套件、但模型檔不完整（只複製了斷詞器）：回報 ModelNotReady，沒有任何連線嘗試。"""
    _real_model_or_skip()
    import shutil

    from media_sorter import classifier

    info = MODEL_PRESETS["standard"]
    source = netpolicy.snapshot_dir(info["tokenizer_repo"], info["tokenizer_revision"])
    cache = tmp_path / "hub"
    shutil.copytree(source, netpolicy.snapshot_dir(info["tokenizer_repo"], info["tokenizer_revision"], cache))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(netpolicy, "MODEL_STAMP_FILE", tmp_path / "stamps.json")
    with pytest.raises(netpolicy.ModelNotReady, match=WEIGHTS_FILE):
        classifier.Classifier("standard", device="cpu")
    assert no_network == [] and netpolicy.is_offline()
