from media_sorter.config import Category, load_categories, load_settings, save_categories, save_settings
from media_sorter.installer import choose_torch_index, parse_nvidia_smi


def test_category_texts_include_name_and_template():
    texts = Category("貓", ["a photo of a cat", " "]).texts()
    assert texts == ["a photo of a cat", "貓", "一張貓的照片"]


def test_categories_roundtrip_and_defaults(tmp_path):
    path = tmp_path / "categories.json"
    assert len(load_categories(path)) >= 10  # 沒有檔案時用預設
    save_categories([Category("寶寶", ["baby"])], path)
    loaded = load_categories(path)
    assert [(c.name, c.prompts) for c in loaded] == [("寶寶", ["baby"])]


def test_settings_merge_with_defaults(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({"video_frames": 3, "unknown": 1}, path)
    settings = load_settings(path)
    assert settings["video_frames"] == 3
    assert "unknown" not in settings
    assert settings["model"] == "auto"


def test_torch_index_selection():
    gpus = parse_nvidia_smi("NVIDIA GeForce RTX 4070, 8.9, 12282\n")
    assert gpus == [("NVIDIA GeForce RTX 4070", 8.9, 12282 / 1024)]
    assert choose_torch_index(gpus).endswith("cu128")
    assert choose_torch_index(parse_nvidia_smi("NVIDIA GeForce GTX 1060, 6.1, 6144")).endswith("cu126")
    assert choose_torch_index([]) is None
