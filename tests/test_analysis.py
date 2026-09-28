import threading

from fakes import FakeClassifier
from helpers import make_image, make_video
from media_sorter.analysis import CONFIRMED, ERROR, PENDING, accept_confident, analyze, rescore
from media_sorter.config import Category

CATEGORIES = [Category("紅"), Category("綠"), Category("藍")]


def test_analyze_images_and_videos(tmp_path):
    paths = [
        make_image(tmp_path / "r.jpg", (250, 0, 0)),
        make_image(tmp_path / "g.png", (0, 250, 0)),
        make_video(tmp_path / "b.mp4", (0, 0, 250)),
    ]
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"nope")
    paths.append(broken)

    seen, progress = [], []
    items = analyze(paths, FakeClassifier(), CATEGORIES, batch_size=2, video_frames=4,
                    on_item=lambda i, item: seen.append(i), on_progress=lambda d, t: progress.append((d, t)))

    assert [item.best(CATEGORIES)[0] for item in items[:3]] == ["紅", "綠", "藍"]
    assert all(item.best(CATEGORIES)[1] > 0.9 for item in items[:3])
    assert items[2].thumbnail  # 影片有預存縮圖
    assert items[3].status == ERROR and items[3].error
    assert sorted(seen) == [0, 1, 2, 3]
    assert progress[-1] == (4, 4)
    assert len(items[0].tags) == 5


def test_stop_event_stops_early(tmp_path):
    paths = [make_image(tmp_path / f"{i}.png", (250, 0, 0)) for i in range(10)]
    stop = threading.Event()
    stop.set()
    items = analyze(paths, FakeClassifier(), CATEGORIES, batch_size=2, stop_event=stop)
    assert not any(item.analyzed for item in items)


def test_rescore_and_accept_confident(tmp_path):
    paths = [make_image(tmp_path / "r.png", (250, 0, 0)), make_image(tmp_path / "b.png", (0, 0, 250))]
    clf = FakeClassifier()
    items = analyze(paths, clf, CATEGORIES)
    items[1].status, items[1].chosen = CONFIRMED, "藍"
    encoded = clf.encoded_images

    fewer = [Category("紅"), Category("綠")]
    rescore(items, clf, fewer)
    assert clf.encoded_images == encoded  # 重算不需要重新讀檔
    assert items[1].status == PENDING and items[1].chosen is None  # 分類被刪除 → 回到待確認
    assert items[0].probs.shape == (2,)

    assert accept_confident(items, fewer, threshold=0.9) == 1
    assert items[0].status == CONFIRMED and items[0].chosen == "紅"
    assert items[1].status == PENDING
