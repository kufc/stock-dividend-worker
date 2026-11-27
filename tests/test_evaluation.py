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
                 "dump_embeddings.py", "collect_holdout.py", "prompt_candidates.py", "real_model_check.py",
                 "embeddings_holdout2_standard.npz", "embeddings_holdout2_accurate.npz",
                 "embeddings_holdout2_video_standard.npz", "embeddings_holdout2_video_accurate.npz",
                 "holdout2_sources.txt", "collect_holdout2.py"):
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


@pytest.mark.parametrize("preset", ["standard", "accurate"])
def test_second_holdout_headline_numbers(preset):
    """第二份獨立測試集：保留原始檔與兩可的檔案，另外有影片。下限一樣很寬，只防向量檔壞掉。"""
    images = evaluate.evaluate(evaluate.load("holdout2", preset))
    assert (images["n"], images["n_known"], images["n_unknown"], images["n_ambiguous"]) == (1342, 908, 434, 99)
    assert len(images["per_class"]) == 13 and min(v["n"] for v in images["per_class"].values()) >= 50
    assert images["closed_balanced"] >= 0.85  # 實測 92.4%／93.2%
    videos = evaluate.evaluate(evaluate.load("holdout2_video", preset))
    assert (videos["n"], videos["n_known"], videos["n_unknown"], videos["n_ambiguous"]) == (231, 167, 64, 33)
    assert videos["closed_accuracy"] >= 0.70  # 實測 85.0%／82.0%


def test_videos_are_scored_from_their_frames_whatever_the_extension():
    """第二份測試集有 22 個 .avi。以前用「檔名是不是 .mp4」判斷影片，.avi 會被當成圖片、不走逐格計分。"""
    from media_sorter.config import DEFAULT_CATEGORIES

    recorded = evaluate.load("holdout2_video", "standard")
    items = recorded.items(list(DEFAULT_CATEGORIES))
    assert any(it.path.suffix == ".avi" for it in items)
    assert all(it.kind == "video" and it.frames is not None and len(it.frames) == 8 for it in items)
    assert all(it.kind == "image" for it in evaluate.load("holdout2", "standard").items(list(DEFAULT_CATEGORIES)))


def test_ambiguous_answers_count_either_category_as_correct():
    class Stub:
        def __init__(self, predictions):
            self.predictions, self.groups = predictions, [""] * len(predictions)

        def answers(self):
            return {"a.jpg": "人像+美食", "b.jpg": "人像+美食", "c.jpg": "人像+美食", "d.jpg": "貓"}

        def items(self, categories):
            class One:
                def __init__(self, name, predicted):
                    self.path, self.predicted = Path(name), predicted

                def best(self, categories):
                    return self.predicted, 0.99

                def is_low(self, *args):
                    return False

            return [One(n, p) for n, p in self.predictions.items()]

    result = evaluate.evaluate(Stub({"a.jpg": "人像", "b.jpg": "美食", "c.jpg": "狗", "d.jpg": "貓"}))
    assert result["closed_accuracy"] == 0.75 and result["n_ambiguous"] == 3
    assert result["per_class"]["人像"]["n"] == 2 and result["per_class"]["美食"]["n"] == 1  # 沒選到的算在第一個答案頭上
    assert result["per_class"]["人像"]["closed"][1] == 0.5 and result["per_class"]["美食"]["closed"][1] == 1.0


def test_readme_quotes_the_second_holdout_numbers():
    """README 引用的是第二份（較難、沒被用來做過任何決定的）獨立測試集；第一份的封閉成績只當對照提一句。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/evaluation.md" in readme
    for preset in ("standard", "accurate"):
        result = evaluate.evaluate(evaluate.load("holdout2", preset))
        assert evaluate.pct(result["closed_balanced"]) in readme
        assert evaluate.pct(result["unknown_false_accept"]) in readme
        assert evaluate.pct(result["accepted_error"]) in readme
        assert evaluate.pct(result["flagged"]) in readme
        assert evaluate.pct(evaluate.evaluate(evaluate.load("holdout2_video", preset))["closed_accuracy"]) in readme
        assert evaluate.pct(evaluate.evaluate(evaluate.load("holdout", preset))["closed_balanced"]) in readme
    assert "準確度明顯較高" not in readme  # 高精準模型在獨立測試集上沒有比較好


def test_default_threshold_and_model_follow_the_documented_decisions(tmp_path):
    """上市版的兩個預設值，以及舊設定檔的升級（docs/evaluation.md「門檻的依據」、docs/roadmap.md「已經做的決定」）。"""
    import json

    from media_sorter import config

    # （外部審查 A7：原本寫死 0.7。門檻的數值由下面兩個「結果」測試把關；這裡只管評測用的門檻就是產品的預設值，
    # 以及舊設定檔會升級到現在的預設值。）
    default = config.DEFAULT_SETTINGS["confidence_threshold"]
    assert default == evaluate.THRESHOLD and 0.5 < default < 1
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"confidence_threshold": 0.5, "model": "auto"}), encoding="utf-8")
    assert config.load_settings(old)["confidence_threshold"] == default    # 舊版的預設值：升級
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


@pytest.mark.parametrize("preset,unknown_limit", [("standard", 0.36), ("accurate", 0.42)])
def test_judging_rules_keep_their_measured_results_on_the_dev_set(preset, unknown_limit):
    """外部審查 A7：釘「期望」而不是「做法」。不管之後怎麼改規則（「其他」怎麼比、門檻多少、相似度下限），
    在開發集上都不能比現在差：實測 未知誤接受 34.8%／40.6%、分對被多標 3.7%／3.1%、需檢查 27.7%／25.0%、
    各分類平均分對 97.1%／96.9%。改善判讀的人可以放心改做法，只要這些結果守得住（變好的話請把上限往下修）。"""
    result = evaluate.evaluate(evaluate.load("dev", preset))
    assert result["unknown_false_accept"] <= unknown_limit
    assert result["correct_flagged"] <= 0.05
    assert result["flagged"] <= 0.30
    assert result["closed_balanced"] >= 0.96
