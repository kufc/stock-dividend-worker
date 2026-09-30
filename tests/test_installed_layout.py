"""安裝版（setup.exe）的資料位置，以及安裝時 pip 憑證元件當掉的自動備援。"""

from pathlib import Path

from media_sorter import config, installer


def test_installed_version_keeps_user_data_outside_the_program_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("MEDIA_SORTER_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(config, "INSTALLED", True)
    assert config.default_data_dir() == tmp_path / "AI Media Sorter"
    monkeypatch.setattr(config, "INSTALLED", False)
    assert config.default_data_dir() == config.ROOT  # 免安裝版：跟程式放在一起
    monkeypatch.setenv("MEDIA_SORTER_HOME", str(tmp_path / "custom"))
    assert config.default_data_dir() == tmp_path / "custom"  # 明確指定的優先


def test_data_folder_gets_the_marker_the_uninstaller_requires(tmp_path):
    data = tmp_path / "AI Media Sorter"
    config.ensure_data_dir(data)
    assert (data / config.DATA_MARKER).is_file()
    config.ensure_data_dir(config.ROOT)  # 程式資料夾本身不需要標記，也不會建立
    assert not (config.ROOT / config.DATA_MARKER).exists()
    from media_sorter import uninstaller

    assert uninstaller.DATA_MARKER == config.DATA_MARKER  # 兩邊要一致
    assert uninstaller.validate_data_dir(data) is None


def test_marker_and_hints_follow_the_layout():
    assert installer.INSTALL_MARKER == (
        config.ROOT / "install-ok.txt" if config.INSTALLED else config.ROOT / ".venv" / "install-ok.txt")
    assert ("修復" in installer.FIX_HINT) == config.INSTALLED


class Recorder:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    def __call__(self, cmd, *a, **k):
        self.calls.append(cmd)
        return self.codes.pop(0)


def test_pip_falls_back_to_bundled_certificates_when_it_fails(monkeypatch, capsys):
    monkeypatch.setattr(installer, "LEGACY_CERTS", False)
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "pip_version", lambda: (25, 0, 1))
    rec = Recorder([1, 0])
    monkeypatch.setattr(installer.subprocess, "call", rec)
    assert installer.pip("torch") is True
    assert "--use-deprecated=legacy-certs" not in rec.calls[0] and "--use-deprecated=legacy-certs" in rec.calls[1]
    assert installer.LEGACY_CERTS is True and "內建的憑證" in capsys.readouterr().out
    rec2 = Recorder([0])
    monkeypatch.setattr(installer.subprocess, "call", rec2)
    installer.pip("numpy")  # 之後的安裝直接用備援，不必再當掉一次
    assert "--use-deprecated=legacy-certs" in rec2.calls[0]
    assert "--retries" in rec2.calls[0] and "--timeout" in rec2.calls[0]
    monkeypatch.setattr(installer, "LEGACY_CERTS", False)


def test_pip_does_not_retry_forever_or_use_unsupported_flag(monkeypatch, capsys):
    monkeypatch.setattr(installer, "LEGACY_CERTS", False)
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "pip_version", lambda: (23, 3, 1))  # 舊版 pip 沒有這個選項
    rec = Recorder([1])
    monkeypatch.setattr(installer.subprocess, "call", rec)
    assert installer.pip("torch") is False and len(rec.calls) == 1
    monkeypatch.setattr(installer, "pip_version", lambda: (25, 0, 1))
    monkeypatch.setattr(installer, "LEGACY_CERTS", True)
    rec = Recorder([3221225477])  # 備援也當掉：只試一次，並說明可能的原因
    monkeypatch.setattr(installer.subprocess, "call", rec)
    assert installer.pip("torch") is False and len(rec.calls) == 1
    assert "存取違規" in capsys.readouterr().out
    monkeypatch.setattr(installer, "LEGACY_CERTS", False)


class Proc:
    def __init__(self, returncode, stderr=""):
        self.returncode, self.stderr = returncode, stderr


def test_truststore_probe_only_reports_real_crashes(monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "win32")
    monkeypatch.setattr(installer, "pip_version", lambda: (25, 0, 1))
    for proc, expected in ((Proc(3221225477), True),
                           (Proc(1, "OSError: exception: access violation writing 0x0000000000000048"), True),
                           (Proc(1, "URLError: no network"), False),  # 一般網路錯誤不算，不需要改用備援
                           (Proc(0), False)):
        monkeypatch.setattr(installer.subprocess, "run", lambda *a, _p=proc, **k: _p)
        assert installer.truststore_crashes() is expected
    monkeypatch.setattr(installer, "pip_version", lambda: (23, 3, 1))
    assert installer.truststore_crashes() is False  # 舊版 pip 沒有這個問題
    monkeypatch.setattr(installer.sys, "platform", "linux")
    monkeypatch.setattr(installer, "pip_version", lambda: (25, 0, 1))
    assert installer.truststore_crashes() is False


def test_install_bat_no_longer_calls_pip_directly():
    text = (Path(config.ROOT) / "install.bat").read_text(encoding="utf-8")
    assert "-m pip install" not in text  # pip 的呼叫都集中在 installer.py（含憑證備援）


def test_pip_never_leaves_a_download_cache(monkeypatch):
    monkeypatch.setattr(installer, "LEGACY_CERTS", False)
    rec = Recorder([0])
    monkeypatch.setattr(installer.subprocess, "call", rec)
    installer.pip("numpy")
    assert "--no-cache-dir" in rec.calls[0]


def test_missing_components_are_explained_and_repair_is_offered(monkeypatch, tmp_path):
    import media_sorter.__main__ as entry

    monkeypatch.setattr(entry, "missing_packages", lambda: ["PIL", "numpy"])
    monkeypatch.setattr(config, "INSTALLED", True)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    (tmp_path / "setup-deps.cmd").write_text("x")
    asked, started = [], []
    monkeypatch.setattr(entry, "_ask_yes_no", lambda text: asked.append(text) or True)
    monkeypatch.setattr(entry.subprocess, "Popen", lambda cmd, **k: started.append(cmd))
    assert entry._required_packages_present() is False
    assert "AI 元件還沒有安裝完成" in asked[0] and "PIL" in asked[0]
    assert started and started[0][-2].endswith("setup-deps.cmd")
    shown = []
    monkeypatch.setattr(config, "INSTALLED", False)
    monkeypatch.setattr(entry, "_show_error_box", shown.append)
    assert entry._required_packages_present() is False and "install.bat" in shown[0]
    monkeypatch.setattr(entry, "missing_packages", lambda: [])
    assert entry._required_packages_present() is True
