"""安裝程式的 pip 限制檔：Windows 上曾因為 torch==2.14.0+cpu 這種帶本機標籤的寫法而無法安裝。"""

import subprocess
import sys
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from media_sorter.installer import public_version, torch_constraints

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("raw, expected", [
    ("2.14.0+cpu", "2.14.0"),        # Windows 上 PyPI 的 CPU 版
    ("2.14.0+cu128", "2.14.0"),      # PyTorch 官方 CUDA 版
    ("0.29.0+cu126", "0.29.0"),
    ("2.14.0", "2.14.0"),            # macOS／Linux 的一般版本
    ("  2.14.0+cpu\n", "2.14.0"),    # 輸出可能帶空白與換行
])
def test_public_version_drops_local_label(raw, expected):
    assert public_version(raw) == expected


def test_constraints_never_contain_local_labels():
    text = torch_constraints("2.14.0+cpu", "0.29.0+cpu")
    assert text == "torch==2.14.0\ntorchvision==0.29.0\n"
    assert "+" not in text


@pytest.mark.parametrize("installed", ["2.14.0+cpu", "2.14.0+cu128", "2.14.0"])
def test_constraint_matches_the_installed_build(installed):
    """依 PEP 440，不含標籤的 == 會比對到所有本機標籤變體，所以不會要求 pip 換掉已裝好的版本。"""
    spec = SpecifierSet(f"=={public_version(installed)}")
    assert spec.contains(installed)
    if "+" in installed:  # 舊寫法（帶標籤）：pip 找不到符合的版本，就是 Windows 上安裝失敗的原因
        assert not SpecifierSet(f"=={installed}").contains("2.14.0")


def test_old_style_constraint_is_what_broke_windows():
    assert not SpecifierSet("==2.14.0+cpu").contains("2.14.0")


@pytest.mark.skipif(not (ROOT / "requirements.txt").exists(), reason="沒有 requirements.txt")
def test_generated_constraints_resolve_with_real_pip(tmp_path):
    """用 pip 自己的解析器驗證：以「目前環境實際安裝的 torch」產生的限制檔，搭配 requirements.txt 能夠解析。

    只解析、不安裝（--dry-run）。需要網路可以連到 PyPI；連不上就略過，不算失敗。
    """
    try:
        import torch
        import torchvision
    except ImportError:
        pytest.skip("這個環境沒有安裝 torch")
    constraints = tmp_path / "constraints.txt"
    constraints.write_text(torch_constraints(torch.__version__, torchvision.__version__), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--disable-pip-version-check",
         "-r", str(ROOT / "requirements.txt"), "-c", str(constraints)],
        capture_output=True, text=True, timeout=300,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0 and any(t in output for t in ("Could not find a version", "Connection", "ProxyError",
                                                          "Temporary failure", "timed out")):
        pytest.skip("連不上 PyPI，無法驗證")
    assert proc.returncode == 0, output[-1500:]
    assert "ResolutionImpossible" not in output
