"""第九輪外部審查（P1）：相依套件、PyTorch 與模型的版本都要固定，安裝不能用「升級到最新」的流程。"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from media_sorter import installer
from media_sorter.config import MODEL_PRESETS

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.!+-]*)$")


def requirement_lines(name):
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_requirements_are_exact_pins():
    lines = requirement_lines("requirements.txt")
    assert len(lines) >= 7
    for line in lines:
        assert PIN.match(line), f"不是固定版本：{line}"
    assert not any(canonical(PIN.match(line).group(1)) in ("torch", "torchvision") for line in lines)


def test_lock_file_pins_everything_with_hashes_and_matches_requirements():
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    entries = re.findall(r"^([A-Za-z0-9_.-]+)==(\S+) \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, flags=re.M)
    locked = {canonical(name): version for name, version, _ in entries}
    assert len(locked) >= 40
    assert len(entries) == len(re.findall(r"^[A-Za-z0-9_.-]+==", text, flags=re.M))  # 每一個套件都有雜湊
    for line in requirement_lines("requirements.txt"):
        name, version = PIN.match(line).groups()
        assert locked.get(canonical(name)) == version, f"{name}：鎖定檔跟 requirements.txt 不一致，請執行 tools/update_lock.py"
    assert "torch" not in locked and "torchvision" not in locked  # 依顯示卡另外安裝（TORCH_PINS）
    assert {"sympy", "networkx", "filelock", "jinja2"} <= set(locked)  # torch 需要的其他套件要在鎖定檔裡（--no-deps 安裝）


def test_torch_is_pinned_per_build_with_hashes():
    assert set(installer.TORCH_PINS) == {installer.CUDA_NEW, installer.CUDA_OLD, "cpu"}
    for build, pins in installer.TORCH_PINS.items():
        for name in ("torch", "torchvision"):
            version, digest = pins[name]
            assert re.fullmatch(r"\d+\.\d+\.\d+", version) and re.fullmatch(r"[0-9a-f]{64}", digest)
        hashed = installer.torch_requirements(build, with_hashes=True).splitlines()
        assert hashed[0] == f"torch=={pins['torch'][0]}+{build} --hash=sha256:{pins['torch'][1]}"
        assert all("--hash=sha256:" in line for line in hashed)
        assert "--hash" not in installer.torch_requirements(build, with_hashes=False)


def test_installed_torch_is_reused_only_when_it_is_the_pinned_version():
    pinned = installer.TORCH_PINS["cu128"]["torch"][0]
    assert installer.torch_is_pinned_build((f"{pinned}+cu128", "12.8"), "cu128", "12.8")
    assert not installer.torch_is_pinned_build(("2.99.0+cu128", "12.8"), "cu128", "12.8")  # 以前只看 CUDA 版本
    assert not installer.torch_is_pinned_build((f"{pinned}+cu128", "12.8"), "cpu", None)
    assert not installer.torch_is_pinned_build(None, "cpu", None)


def run_installer(monkeypatch, *, hashed: bool, gpus, torch_install_ok=True):
    """跑一遍 installer.main()，把 pip 換成記錄器：看它實際會下什麼指令。"""
    calls = []
    files = {}

    def fake_pip(*args):
        calls.append(args)
        for i, arg in enumerate(args):
            if arg == "-r":
                files[args[i + 1]] = Path(args[i + 1]).read_text(encoding="utf-8")
        if "--index-url" in args and not torch_install_ok and "cpu" not in args[-1]:
            return False
        return True

    state = {"torch": None}
    monkeypatch.setattr(installer, "pip", fake_pip)
    monkeypatch.setattr(installer, "hashes_apply", lambda: hashed)
    monkeypatch.setattr(installer, "detect_nvidia", lambda: (gpus, "560.1" if gpus else None))
    monkeypatch.setattr(installer, "app_is_running", lambda: False)
    monkeypatch.setattr(installer, "truststore_crashes", lambda: False)
    monkeypatch.setattr(installer, "installed_torch", lambda: state["torch"])
    monkeypatch.setattr(installer, "run_python", lambda code: (0, "2.11.0+cu128\n0.26.0+cu128", ""))
    monkeypatch.setattr(installer.subprocess, "call", lambda *a, **k: 0)
    return calls, files, state


def test_installer_never_uses_upgrade_and_requires_hashes_on_the_bundled_python(monkeypatch, tmp_path):
    calls, files, state = run_installer(monkeypatch, hashed=True, gpus=[("RTX 5070", 12.0, 12.0)])
    monkeypatch.setattr(installer, "INSTALL_MARKER", tmp_path / "install-ok.txt")
    monkeypatch.setattr(installer, "INSTALLED", True)
    orig = installer.install_torch

    def install_and_remember(build):
        ok = orig(build)
        state["torch"] = (f"{installer.TORCH_PINS[build]['torch'][0]}+{build}", "12.8")
        return ok

    monkeypatch.setattr(installer, "install_torch", install_and_remember)
    assert installer.main() == 0
    flat = [arg for call in calls for arg in call]
    assert "--upgrade" not in flat and "-U" not in flat  # 以前：pip install --upgrade torch torchvision
    torch_call, lock_call = calls[0], calls[1]
    assert "--require-hashes" in torch_call and "--no-deps" in torch_call
    assert torch_call[-1] == "https://download.pytorch.org/whl/cu128"
    pinned = installer.TORCH_PINS["cu128"]
    torch_file = next(text for name, text in files.items() if name != "_marker" and "torch==" in text)
    assert f"torch=={pinned['torch'][0]}+cu128 --hash=sha256:{pinned['torch'][1]}" in torch_file
    assert lock_call == ("--require-hashes", "--no-deps", "-r", str(installer.LOCK_FILE))


def test_other_pythons_install_exact_pins_without_hashes(monkeypatch, tmp_path):
    calls, files, state = run_installer(monkeypatch, hashed=False, gpus=[])
    monkeypatch.setattr(installer, "INSTALL_MARKER", tmp_path / "install-ok.txt")
    monkeypatch.setattr(installer, "INSTALLED", True)
    state["torch"] = None
    orig = installer.install_torch
    monkeypatch.setattr(installer, "install_torch",
                        lambda build: (orig(build), state.update(torch=("2.15.0+cpu", None)))[0])
    # 這個測試講的是「不是 Windows」的環境（開發用）。Windows 上沒有雜湊可核對時安裝程式會直接拒絕
    # （test_review10 另外測），所以在 Windows 上跑這個測試時要假裝是別的系統，否則它測到的是那個拒絕。
    monkeypatch.setattr(installer.sys, "platform", "linux")
    assert installer.main() == 0
    flat = [arg for call in calls for arg in call]
    assert "--upgrade" not in flat and "--require-hashes" not in flat
    assert calls[0][-1] == "https://download.pytorch.org/whl/cpu"
    assert any(str(ROOT / "requirements.txt") in call for call in calls)


def test_cuda_failure_falls_back_to_the_pinned_cpu_build(monkeypatch, tmp_path):
    calls, files, state = run_installer(monkeypatch, hashed=True, gpus=[("GTX 1060", 6.1, 6.0)], torch_install_ok=False)
    monkeypatch.setattr(installer, "INSTALL_MARKER", tmp_path / "install-ok.txt")
    monkeypatch.setattr(installer, "INSTALLED", True)
    orig = installer.install_torch
    monkeypatch.setattr(installer, "install_torch",
                        lambda build: (ok := orig(build), state.update(torch=("2.15.0+cpu", None)) if ok else None)[0])
    assert installer.main() == 0
    indexes = [call[-1] for call in calls if "--index-url" in call]
    assert indexes == ["https://download.pytorch.org/whl/cu126", "https://download.pytorch.org/whl/cpu"]


def test_models_are_pinned_to_commits():
    for info in MODEL_PRESETS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", info["hf_revision"]) and re.fullmatch(r"[0-9a-f]{40}", info["tokenizer_revision"])


def test_installer_bundles_the_lock_file_and_python_312():
    source = (ROOT / "installer" / "build_installer.py").read_text(encoding="utf-8")
    assert '"requirements.lock"' in source and '"python 3.12.*"' in source


def test_readme_states_the_installer_is_unsigned_and_how_to_update_pins():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "安裝程式沒有數位簽章" in readme and "這個警告是正確的" in readme
    assert "仍要執行" not in readme  # 不再教使用者略過警告
    for name in ("tools/update_lock.py", "tools/update_model_pins.py", "TORCH_PINS", "requirements.lock"):
        assert name in readme


@pytest.mark.skipif(sys.platform != "linux", reason="只在開發環境驗證鎖定檔能被 pip 接受")
def test_pip_accepts_the_lock_file_for_windows(tmp_path):
    """讓 pip 以「Windows x64、Python 3.12」為對象解析鎖定檔（只下載最小的一個套件來確認雜湊格式被接受）。"""
    one = tmp_path / "one.txt"
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    match = re.search(r"^send2trash==.*?(?=^\S|\Z)", text, flags=re.M | re.S)
    one.write_text(match.group(0), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "pip", "download", "--require-hashes", "--no-deps", "--disable-pip-version-check",
                           "--platform", "win_amd64", "--python-version", "3.12", "--only-binary=:all:",
                           "-r", str(one), "-d", str(tmp_path / "dl")], capture_output=True, text=True, timeout=300)
    output = proc.stdout + proc.stderr
    if proc.returncode != 0 and any(t in output for t in ("Connection", "ProxyError", "Temporary failure", "timed out",
                                                          "No module named pip")):
        pytest.skip("連不上 PyPI 或沒有 pip，無法驗證")
    assert proc.returncode == 0, output[-1500:]
