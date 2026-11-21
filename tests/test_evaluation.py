"""docs/evaluation.md 的數字要能重現：文件裡的表格必須等於 tools/evaluate.py 現在算出來的。

獨立測試集（holdout）只用來報告。這裡的斷言只有「文件跟程式一致」與幾個很寬的下限（避免向量檔壞掉沒人發現），
**不要為了讓成績好看而回頭調描述或門檻**——那樣它就不再是獨立測試集（見 docs/evaluation.md）。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import evaluate  # noqa: E402


def test_evaluation_data_is_in_the_repo():
    """第九輪審查：交付的 ZIP 少了這些檔案，「97%」無法重現。現在缺檔是失敗，不是 skip。"""
    for name in ("embeddings_standard.npz", "embeddings_accurate.npz", "embeddings_holdout_standard.npz",
                 "embeddings_holdout_accurate.npz", "photo_sources.txt", "holdout_sources.txt",
                 "dump_embeddings.py", "collect_holdout.py", "prompt_candidates.py", "real_model_check.py"):
        assert (evaluate.MANUAL / name).is_file(), name


def test_document_matches_what_the_tool_computes_now():
    doc = evaluate.DOC.read_text(encoding="utf-8")
    generated = doc[doc.index(evaluate.BEGIN) + len(evaluate.BEGIN):doc.index(evaluate.END)].strip()
    assert generated == evaluate.render().strip(), "請執行 python tools/evaluate.py --update-doc"


@pytest.mark.parametrize("preset", ["standard", "accurate"])
def test_holdout_headline_numbers(preset):
    result = evaluate.evaluate(evaluate.load("holdout", preset))
    assert result["n"] == 1176 and result["n_known"] == 752 and result["n_unknown"] == 424
    assert len(result["per_class"]) == 13 and min(v["n"] for v in result["per_class"].values()) >= 50
    assert result["closed_balanced"] >= 0.90  # 很寬的下限：實測 97.5%／96.8%
    assert 0.0 < result["unknown_false_accept"] < 1.0


def test_readme_quotes_the_holdout_numbers():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/evaluation.md" in readme
    for preset in ("standard", "accurate"):
        result = evaluate.evaluate(evaluate.load("holdout", preset))
        assert evaluate.pct(result["closed_balanced"]) in readme
        assert evaluate.pct(result["unknown_false_accept"]) in readme
        assert evaluate.pct(result["accepted_error"]) in readme
    assert "準確度明顯較高" not in readme  # 高精準模型在獨立測試集上沒有比較好


def test_default_threshold_and_model_follow_the_documented_decisions(tmp_path):
    """上市版的兩個預設值，以及舊設定檔的升級（docs/evaluation.md「門檻的依據」、docs/roadmap.md「已經做的決定」）。"""
    import json

    from media_sorter import config

    assert config.DEFAULT_SETTINGS["confidence_threshold"] == 0.7 == evaluate.THRESHOLD
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"confidence_threshold": 0.5, "model": "auto"}), encoding="utf-8")
    assert config.load_settings(old)["confidence_threshold"] == 0.7        # 舊版的預設值：升級
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"confidence_threshold": 0.35}), encoding="utf-8")
    assert config.load_settings(custom)["confidence_threshold"] == 0.35    # 使用者自己調過的：不動
    chosen = tmp_path / "chosen.json"
    chosen.write_text(json.dumps({"confidence_threshold": 0.5, "defaults_version": 2}), encoding="utf-8")
    assert config.load_settings(chosen)["confidence_threshold"] == 0.5     # 升級之後特地選 50 的：尊重
    config.save_settings(config.load_settings(old), old)
    assert json.loads(old.read_text(encoding="utf-8"))["defaults_version"] == 2


@pytest.mark.parametrize("preset", ["standard", "accurate"])
def test_default_threshold_on_the_dev_set(preset):
    """門檻 70 的依據是開發集：分對的檔案少於 5% 被多標，不屬於任何分類的檔案被接受的比例明顯低於門檻 50 時。"""
    recorded = evaluate.load("dev", preset)
    now, before = evaluate.evaluate(recorded), evaluate.evaluate(recorded, 0.5)
    assert now["correct_flagged"] <= 0.05
    assert now["unknown_false_accept"] <= before["unknown_false_accept"] - 0.08
    assert now["accepted_error"] < before["accepted_error"]
