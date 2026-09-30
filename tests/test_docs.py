"""中英對照的「使用說明與免責聲明」：格式要能在記事本正確顯示，且內容要跟程式保持同步。"""

import re
from pathlib import Path

import pytest

from media_sorter import __version__
from media_sorter.organizer import STAGE_MARK_COPY, STAGE_MARK_ORIGINAL

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "使用說明與免責聲明_Usage-and-Disclaimer.txt"
CJK = re.compile(r"[一-鿿]")


@pytest.fixture(scope="module")
def raw() -> bytes:
    return DOC.read_bytes()


@pytest.fixture(scope="module")
def text(raw) -> str:
    return raw.decode("utf-8-sig")


def test_notepad_friendly_encoding(raw):
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM：舊版記事本也能正確顯示中文
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")  # 全部是 Windows 換行


def test_every_chinese_item_has_an_english_counterpart(text):
    blocks = [b for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if b.startswith("・")]
    assert len(blocks) >= 40
    for block in blocks:
        lines = block.split("\n")
        chinese = [ln for ln in lines if CJK.search(ln) and not ln.startswith("   ")]
        english = [ln for ln in lines if ln.startswith("   ") and re.search(r"[A-Za-z]{3,}", ln)]
        assert chinese and english, f"這一項缺少中文或英文：\n{block}"


def test_sections_are_bilingual(text):
    headings = re.findall(r"^(\d+)\. (.+)\n   (.+)$", text, flags=re.M)
    assert [h[0] for h in headings] == [str(i) for i in range(1, 13)]
    for number, zh, en in headings:
        assert CJK.search(zh) and re.search(r"[A-Za-z]{3,}", en), f"第 {number} 節標題不是中英對照"


def test_version_matches_program(text):
    assert f"版本 Version：{__version__}" in text


def test_names_match_the_program(text):
    app_source = "".join((ROOT / "media_sorter" / name).read_text(encoding="utf-8")
                         for name in ("app.py", "views.py", "dialogs.py"))
    for label in ("開始辨識", "開始複製", "確認並看下一個", "完成目前檔案後停止", "整理紀錄", "需檢查",
                  "移除這次建立的複本", "將這批原檔移到回收筒", "移到資源回收筒", "已分類"):
        assert label in text, f"說明裡沒有提到「{label}」"
        assert label in app_source, f"程式裡已經沒有「{label}」，說明需要更新"
    assert f"({STAGE_MARK_ORIGINAL})" in text  # 資源回收筒裡的檔名標記，要跟程式一致
    assert f"({STAGE_MARK_COPY})" in text


def test_mentioned_files_exist(text):
    for name in ("install.bat", "start.bat", "README.md"):
        assert name in text and (ROOT / name).exists()


def test_disclaimer_covers_the_essentials(text):
    for phrase in ("as is", "Limitation of liability", "責任限制", "Your responsibility", "你的責任",
                   "Back up", "備份", "Third-party", "第三方", "not legal advice", "不是法律意見"):
        assert phrase in text, phrase


def test_readme_links_to_the_document():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert DOC.name in readme
