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
