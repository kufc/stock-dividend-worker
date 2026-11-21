"""第九輪外部審查（P1）：辨識結果與人工修正要保存，程式被關掉、強制結束後可以接續。

驗收：辨識到一半強制結束行程，重開後已完成的不用重算、人工修改還在；只對新增或變更的檔案重新辨識。
"""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from conftest import pump, run_analysis
from fakes import FakeClassifier
from helpers import make_image, make_video
from media_sorter import analysis
from media_sorter.analysis import CONFIRMED, PENDING, SKIPPED, analyze, model_key
from media_sorter.config import Category
from media_sorter.workdb import DB_NAME, WorkStore, file_fingerprint

CATS = [Category("紅"), Category("綠"), Category("藍")]
COLORS = [(250, 10, 10), (10, 250, 10), (10, 10, 250)]
ROOT = Path(__file__).resolve().parent.parent


def photos(folder, n):
    return [make_image(folder / f"IMG_{i:03d}.png", COLORS[i % 3]) for i in range(n)]


@pytest.fixture
def store(tmp_path):
    s = WorkStore(tmp_path / "cache")
    yield s
    s.close()


# ---------------------------------------------------------------------------- 接續：沒變過的不重算
def test_second_run_reuses_everything_and_gives_identical_results(tmp_path, store):
    paths = photos(tmp_path / "src", 7) + [make_video(tmp_path / "src" / "v.mp4", (250, 10, 10))]
    first_clf = FakeClassifier()
    first = analyze(paths, first_clf, CATS, store=store, workers=2)
    assert first_clf.encoded_images > 0 and not any(it.cached for it in first)

    second_clf = FakeClassifier()
    second = analyze(paths, second_clf, CATS, store=store, workers=2)
    assert second_clf.encoded_images == 0 and all(it.cached for it in second)  # 完全沒有進模型
    for a, b in zip(first, second, strict=True):
        assert np.array_equal(a.embedding, b.embedding) and np.array_equal(a.probs, b.probs)
        assert a.date == b.date and a.best(CATS) == b.best(CATS)
        assert (a.frames is None) == (b.frames is None) and (a.frames is None or np.array_equal(a.frames, b.frames))


def test_second_run_does_not_even_open_the_files(tmp_path, store, monkeypatch):
    paths = photos(tmp_path / "src", 4)
    analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    monkeypatch.setattr(analysis, "load_media", lambda *a, **k: pytest.fail("不應該再讀檔"))
    assert all(it.analyzed for it in analyze(paths, FakeClassifier(), CATS, store=store, workers=2))


def test_only_new_and_changed_files_are_analysed_again(tmp_path, store):
    paths = photos(tmp_path / "src", 6)
    analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    make_image(paths[1], (10, 10, 250), size=(80, 60))            # 內容換了（大小、時間都變）
    st = paths[2].stat()
    os.utime(paths[2], ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # 只有修改時間變
    new = make_image(tmp_path / "src" / "NEW.png", (10, 250, 10))  # 新增的
    clf = FakeClassifier()
    items = analyze(paths + [new], clf, CATS, store=store, workers=2)
    assert {it.path.name for it in items if not it.cached} == {paths[1].name, paths[2].name, "NEW.png"}
    assert clf.encoded_images == 3
    assert items[1].best(CATS)[0] == "藍"  # 用的是新內容的結果


def test_model_or_preprocessing_change_invalidates_saved_results(tmp_path, store, monkeypatch):
    paths = photos(tmp_path / "src", 3)
    analyze(paths, FakeClassifier(), CATS, store=store, workers=2)

    class OtherModel(FakeClassifier):
        model_name = "another-model"

    assert not any(it.cached for it in analyze(paths, OtherModel(), CATS, store=store, workers=2))
    assert all(it.cached for it in analyze(paths, OtherModel(), CATS, store=store, workers=2))

    class NewRevision(OtherModel):
        model_revision = "abcdef123456"

    assert not any(it.cached for it in analyze(paths, NewRevision(), CATS, store=store, workers=2))
    monkeypatch.setattr(analysis, "PREPROCESS_VERSION", analysis.PREPROCESS_VERSION + 1)
    assert not any(it.cached for it in analyze(paths, NewRevision(), CATS, store=store, workers=2))
    assert "prep" in model_key(NewRevision()) and "abcdef123456" in model_key(NewRevision())


def test_video_frame_count_is_part_of_the_key(tmp_path, store):
    video = make_video(tmp_path / "src" / "v.mp4", (250, 10, 10))
    analyze([video], FakeClassifier(), CATS, store=store, video_frames=4, workers=2)
    assert analyze([video], FakeClassifier(), CATS, store=store, video_frames=4, workers=2)[0].cached
    assert not analyze([video], FakeClassifier(), CATS, store=store, video_frames=8, workers=2)[0].cached


def test_unreadable_files_are_not_cached_and_are_retried(tmp_path, store):
    bad = tmp_path / "src" / "bad.png"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not an image")
    assert analyze([bad], FakeClassifier(), CATS, store=store, workers=2)[0].status == analysis.ERROR
    make_image(bad, (250, 10, 10))
    item = analyze([bad], FakeClassifier(), CATS, store=store, workers=2)[0]
    assert item.analyzed and not item.cached


# ---------------------------------------------------------------------------- 縮圖在磁碟，不在記憶體
def test_thumbnails_live_on_disk_not_in_memory(tmp_path, store):
    paths = photos(tmp_path / "src", 5)
    items = analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    for item in items:
        assert item.thumbnail is None and item.thumb_file.is_file()  # 記憶體裡沒有 JPEG 位元組
        assert item.thumbnail_data().startswith(b"\xff\xd8") and item.has_thumbnail
    again = analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    assert [a.thumb_file for a in again] == [b.thumb_file for b in items] and again[0].thumbnail is None
    without_store = analyze(paths, FakeClassifier(), CATS, workers=2)
    assert without_store[0].thumbnail and without_store[0].thumb_file is None  # 沒有資料庫時維持原本的行為


def test_missing_thumbnail_file_forces_reanalysis(tmp_path, store):
    paths = photos(tmp_path / "src", 2)
    items = analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    items[0].thumb_file.unlink()
    again = analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    assert [it.cached for it in again] == [False, True] and again[0].thumb_file.is_file()


def test_changed_file_replaces_its_old_thumbnail(tmp_path, store):
    path = photos(tmp_path / "src", 1)[0]
    old = analyze([path], FakeClassifier(), CATS, store=store, workers=2)[0].thumb_file
    make_image(path, (10, 10, 250), size=(90, 70))
    new = analyze([path], FakeClassifier(), CATS, store=store, workers=2)[0].thumb_file
    assert new != old and new.is_file() and not old.exists()
    assert len(list((tmp_path / "cache" / "thumbs").rglob("*.jpg"))) == 1


# ---------------------------------------------------------------------------- 人工修改
def test_decisions_are_saved_immediately_and_restored(tmp_path, store):
    paths = photos(tmp_path / "src", 4)
    analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    store.save_decision(paths[0], CONFIRMED, "藍")
    store.save_decision(paths[1], SKIPPED, None)
    store.save_decision(paths[2], CONFIRMED, "已經刪掉的分類")
    # 另開一條連線讀：確認是「當下就 commit」，不是留在記憶體裡
    other = sqlite3.connect(tmp_path / "cache" / DB_NAME)
    assert other.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 3
    other.close()
    items = analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    assert (items[0].status, items[0].chosen) == (CONFIRMED, "藍")
    assert (items[1].status, items[1].chosen) == (SKIPPED, None)
    assert items[2].status == PENDING and items[3].status == PENDING  # 分類不在了：不套用
    store.save_decision(paths[0], PENDING, None)  # 撤回
    assert analyze(paths, FakeClassifier(), CATS, store=store, workers=2)[0].status == PENDING


def test_decision_is_dropped_when_the_file_content_changes(tmp_path, store):
    path = photos(tmp_path / "src", 1)[0]
    analyze([path], FakeClassifier(), CATS, store=store, workers=2)
    store.save_decision(path, CONFIRMED, "綠")
    make_image(path, (10, 10, 250), size=(90, 70))
    item = analyze([path], FakeClassifier(), CATS, store=store, workers=2)[0]
    assert item.status == PENDING and store.load_decision(path) is None


def test_item_watcher_fires_on_every_decision_change(tmp_path):
    seen = []
    item = analysis.Item(tmp_path / "a.png", "image")
    item.watcher = lambda it: seen.append((it.status, it.chosen))
    item.status, item.chosen = CONFIRMED, "紅"
    item.status, item.chosen = PENDING, None
    item.error = "x"  # 其他欄位不觸發
    assert seen == [(CONFIRMED, None), (CONFIRMED, "紅"), (PENDING, "紅"), (PENDING, None)]


# ---------------------------------------------------------------------------- 資料庫本身
def test_corrupt_database_is_set_aside_and_work_continues(tmp_path):
    folder = tmp_path / "cache"
    folder.mkdir()
    (folder / DB_NAME).write_bytes(b"this is not a sqlite database" * 100)
    store = WorkStore(folder)
    try:
        assert store.recreated_from is not None and store.recreated_from.exists()  # 壞掉的那份留著
        paths = photos(tmp_path / "src", 2)
        assert all(it.analyzed for it in analyze(paths, FakeClassifier(), CATS, store=store, workers=2))
        assert all(it.cached for it in analyze(paths, FakeClassifier(), CATS, store=store, workers=2))
    finally:
        store.close()


def test_storage_failure_does_not_break_analysis(tmp_path, store, monkeypatch):
    paths = photos(tmp_path / "src", 3)
    monkeypatch.setattr(store, "save_analysis", lambda *a, **k: (_ for _ in ()).throw(OSError(28, "磁碟已滿")))
    items = analyze(paths, FakeClassifier(), CATS, store=store, workers=2)
    assert all(it.analyzed and it.thumbnail for it in items)  # 照常完成，縮圖退回放在記憶體


def test_prune_clear_and_counts(tmp_path, store):
    a = photos(tmp_path / "src" / "a", 3)
    b = photos(tmp_path / "src" / "b_100%", 2)  # 路徑裡有 LIKE 的特殊字元
    analyze(a + b, FakeClassifier(), CATS, store=store, workers=2)
    store.save_decision(a[0], CONFIRMED, "紅")
    assert store.count_cached(tmp_path / "src") == 5 and store.count_cached(tmp_path / "src" / "b_100%") == 2
    assert store.count_cached(tmp_path / "src" / "b") == 0 and store.stats() == (5, 1)
    assert store.prune(max_age_days=30) == 0
    assert store.prune(max_age_days=-1) == 5 and store.stats() == (0, 0)
    assert not list((tmp_path / "cache" / "thumbs").rglob("*.jpg"))
    analyze(a, FakeClassifier(), CATS, store=store, workers=2)
    store.clear()
    assert store.stats() == (0, 0) and not list((tmp_path / "cache" / "thumbs").rglob("*.jpg"))


def test_many_decisions_are_fast_enough_for_bulk_assign(tmp_path, store):
    path = photos(tmp_path / "src", 1)[0]
    fingerprint = file_fingerprint(path)
    start = time.time()
    for i in range(2000):
        store.save_decision(Path(f"{path}.{i}"), CONFIRMED, "紅", fingerprint)
    assert time.time() - start < 20  # 一次選幾千個檔案改分類也不會卡很久


# ---------------------------------------------------------------------------- 驗收：辨識到一半強制結束行程
CHILD = r"""
import sys, time
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/tests")
from pathlib import Path
from fakes import FakeClassifier
from media_sorter.analysis import CONFIRMED, SKIPPED, analyze
from media_sorter.config import Category
from media_sorter.workdb import WorkStore

class Slow(FakeClassifier):
    model_name = "FakeClassifier"  # 跟重開後用的是「同一個模型」（保存的結果綁著模型名稱）

    def encode_prepared(self, tensors):
        time.sleep(0.25)  # 讓主程式有時間在「辨識到一半」時把這個行程殺掉
        return super().encode_prepared(tensors)

store = WorkStore(Path(sys.argv[2]))
paths = sorted(Path(sys.argv[3]).glob("*.png"))
decided = []

def on_item(index, item):
    if len(decided) < 2:  # 使用者在辨識途中就改了前兩個完成的檔案
        store.save_decision(item.path, CONFIRMED if not decided else SKIPPED, "藍" if not decided else None, item.fingerprint)
        decided.append(item.path.name)
    print("DONE", item.path.name, flush=True)

analyze(paths, Slow(), [Category("紅"), Category("綠"), Category("藍")], batch_size=2, workers=2, on_item=on_item, store=store)
print("FINISHED", flush=True)
"""


def test_killing_the_process_midway_loses_nothing_that_was_finished(tmp_path):
    paths = photos(tmp_path / "src", 24)
    cache = tmp_path / "cache"
    proc = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT), str(cache), str(tmp_path / "src")],
                            stdout=subprocess.PIPE, text=True, encoding="utf-8",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    reported = []
    try:
        for line in proc.stdout:
            if line.startswith("DONE"):
                reported.append(line.split()[1])
            if len(reported) >= 8:
                break
        proc.kill()  # 強制結束（Windows：TerminateProcess；其他：SIGKILL），沒有任何收尾的機會
    finally:
        proc.wait(timeout=60)
        proc.stdout.close()
    assert 8 <= len(reported) < len(paths), "行程應該在辨識到一半時被結束"

    store = WorkStore(cache)  # 「重開程式」
    try:
        assert store.recreated_from is None  # 資料庫沒有壞
        clf = FakeClassifier()
        items = analyze(paths, clf, CATS, store=store, workers=2)
        by_name = {it.path.name: it for it in items}
        for name in reported:  # 被殺之前回報「完成」的，每一個都不用重算
            assert by_name[name].cached, name
        reused = sum(it.cached for it in items)
        assert clf.encoded_images == len(paths) - reused < len(paths) - 7
        assert all(it.analyzed and it.thumb_file.is_file() for it in items)
        # 途中做的兩個人工修改都還在
        assert (by_name[reported[0]].status, by_name[reported[0]].chosen) == (CONFIRMED, "藍")
        assert by_name[reported[1]].status == SKIPPED
    finally:
        store.close()


# ---------------------------------------------------------------------------- 介面：重開程式可以接續
def test_app_resumes_after_restart_with_decisions_intact(app_factory, tmp_path):
    src = tmp_path / "photos"
    photos(src, 5)
    app = app_factory()
    run_analysis(app, src)
    first_encoded = app.classifier.encoded_images
    assert first_encoded == 5 and all(it.thumbnail is None and it.thumb_file for it in app.items)
    app.select_bucket("all")
    app.tree.selection_set(app._iid(0))
    app.assign("藍")
    app.tree.selection_set(app._iid(1))
    app.skip_selected()
    files, decisions = app.store.stats()
    assert files == 5 and decisions >= 1

    app = app_factory()  # 關掉再開：全新的 App 物件、全新的模型物件，同一個工作資料庫
    app.folder_var.set(str(src))
    pump(app.root, lambda: app.stats is not None)
    assert app.stats.cached == 5 and "之前辨識過" in app.page1.hint_var.get()
    make_image(src / "NEW.png", (10, 250, 10))
    run_analysis(app, src)
    assert app.classifier.encoded_images == 1  # 只有新增的那個檔案進了模型
    by_name = {it.path.name: it for it in app.items}
    assert (by_name["IMG_000.png"].status, by_name["IMG_000.png"].chosen) == (CONFIRMED, "藍")
    assert "沿用上次保存的結果" in app.status_var.get()
    assert app.image_for(app._iid(0)) is not None  # 縮圖從磁碟快取畫得出來

    # 撤回上一步之類的修改也會立刻寫回去
    item = by_name["IMG_000.png"]
    item.status, item.chosen = PENDING, None
    assert app.store.load_decision(item.path) is None


def test_reanalysis_in_the_same_session_keeps_decisions_without_asking(app, tmp_path, monkeypatch):
    from tkinter import messagebox

    src = tmp_path / "photos"
    photos(src, 3)
    run_analysis(app, src)
    app.select_bucket("all")
    app.tree.selection_set(app._iid(2))
    app.assign("綠")
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: pytest.fail(f"不應該再問：{a}"))
    run_analysis(app, src)
    assert (app.items[2].status, app.items[2].chosen) == (CONFIRMED, "綠")


def test_clear_saved_work_needs_confirmation(app, tmp_path, monkeypatch):
    from tkinter import messagebox

    src = tmp_path / "photos"
    photos(src, 3)
    run_analysis(app, src)
    app.clear_saved_work()  # 預設回答「否」
    assert app.store.stats()[0] == 3
    asked = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, text, **k: (asked.append(text), True)[1])
    app.clear_saved_work()
    assert "不會動到" in asked[0] and app.store.stats() == (0, 0)
    assert all(p.exists() for p in src.iterdir())  # 照片當然都還在
