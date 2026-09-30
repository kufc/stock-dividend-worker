"""解除安裝程式：只能刪清單裡的東西，使用者的檔案（照片、輸出、復原移除）一律不動。"""

import os
import re
import sys
from pathlib import Path

import pytest

from media_sorter import uninstaller as U

ROOT = Path(__file__).resolve().parent.parent
MODEL_B = U.MODEL_CACHE_DIRS[0]


def make_install(base: Path) -> dict:
    """在 base 建一個假的安裝資料夾，另外放使用者的檔案（都不該被動到）。"""
    root = base / "AI媒體分類器"
    for name in U.MARKER_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x")
    (root / ".venv" / "Lib" / "site-packages").mkdir(parents=True)
    (root / ".venv" / "Lib" / "site-packages" / "torch.py").write_text("t" * 1000)
    (root / ".venv" / "Scripts").mkdir()
    (root / ".venv" / "Scripts" / "python.exe").write_text("py")
    (root / ".venv" / "install-ok.txt").write_text("ok")
    (root / "settings.json").write_text("{}")
    (root / "categories.json").write_text("{}")
    logs = root / "logs"
    (logs / U.QUARANTINE_DIR / "20240101_000000").mkdir(parents=True)
    quarantined = logs / U.QUARANTINE_DIR / "20240101_000000" / "複本.jpg"
    quarantined.write_text("你的照片")
    (logs / "整理紀錄_20240101_000000.csv").write_text("log")
    (logs / "已完成_20230101_000000.csv").write_text("log")
    (logs / "error.log").write_text("e")
    (root / "keep.txt").write_text("程式資料夾裡使用者自己放的檔案")
    photos = base / "手機照片"
    (photos / "已分類" / "貓").mkdir(parents=True)
    originals = photos / "IMG_1.jpg"
    originals.write_text("原檔")
    copy = photos / "已分類" / "貓" / "貓_001.jpg"
    copy.write_text("複本")
    return {"root": root, "quarantined": quarantined, "originals": originals, "copy": copy, "photos": photos}


def user_files_intact(env) -> bool:
    return (env["quarantined"].read_text() == "你的照片" and env["originals"].read_text() == "原檔"
            and env["copy"].read_text() == "複本" and (env["root"] / "keep.txt").exists())


def run(env, *args, cache=None, prefix="/nonexistent-python", answers=(), **kw):
    lines = []
    replies = iter(answers)
    code = U.run(list(args), root=env["root"], cache_dir=cache or env["root"].parent / "hf-empty", prefix=prefix,
                 input_func=lambda prompt: (lines.append(prompt), next(replies))[1], say=lines.append, **kw)
    return code, "\n".join(map(str, lines))


@pytest.fixture
def env(tmp_path):
    return make_install(tmp_path)


def test_default_yes_removes_only_the_environment(env):
    code, out = run(env, "--yes")
    root = env["root"]
    assert code == U.EXIT_OK and "解除安裝完成" in out
    assert not (root / ".venv").exists()
    assert (root / "settings.json").exists() and (root / "categories.json").exists()  # 設定預設保留
    assert (root / "logs" / "整理紀錄_20240101_000000.csv").exists()
    assert user_files_intact(env)
    assert (root / "install.bat").exists() and (root / "media_sorter" / "uninstaller.py").exists()  # 程式檔案本身不刪


def test_optional_items_need_their_own_flag_and_quarantine_is_never_touched(env):
    root = env["root"]
    code, _ = run(env, "--yes", "--remove-settings", "--remove-logs")
    assert code == U.EXIT_OK
    assert not (root / "settings.json").exists() and not (root / "categories.json").exists()
    logs = root / "logs"
    assert not list(logs.glob("*.csv")) and not (logs / "error.log").exists()
    assert env["quarantined"].read_text() == "你的照片"  # 「復原移除」是使用者的檔案，永遠保留（logs 資料夾也因此保留）
    assert user_files_intact(env)


def test_dry_run_changes_nothing(env):
    code, out = run(env, "--dry-run", "--remove-settings", "--remove-logs")
    assert code == U.EXIT_OK and "沒有刪除任何東西" in out
    assert (env["root"] / ".venv" / "install-ok.txt").exists() and (env["root"] / "settings.json").exists()
    assert user_files_intact(env)


def test_interactive_needs_explicit_yes_and_defaults_to_keep(env):
    root = env["root"]
    code, out = run(env, answers=["", "", "n"])  # 設定：保留；紀錄：保留；最後確認：取消
    assert code == U.EXIT_FAILED and "已取消" in out
    assert (root / ".venv").exists()  # 取消 → 什麼都沒動

    code, out = run(env, answers=["y", "", "Y"])  # 移除設定、保留紀錄、確認
    assert code == U.EXIT_OK
    assert not (root / ".venv").exists() and not (root / "settings.json").exists()
    assert (root / "logs" / "整理紀錄_20240101_000000.csv").exists()


def test_eof_on_input_cancels(env):
    def eof(prompt):
        raise EOFError

    code = U.run([], root=env["root"], cache_dir=env["root"].parent / "none", prefix="/x", input_func=eof,
                 say=lambda s: None)
    assert code == U.EXIT_FAILED and (env["root"] / ".venv").exists()


def test_plan_lists_sizes_warns_about_active_records_and_says_what_is_safe(env):
    code, out = run(env, "--dry-run")
    assert "照片與影片" in out and "已分類" in out and "資源回收筒" in out and "復原移除" in out
    assert "1 份整理紀錄還沒處理完" in out  # 只有「整理紀錄_*」算還沒處理完；「已完成_*」不算
    assert "大小：不到 1 MB" in out  # 會列出大小


def test_refuses_when_the_folder_is_not_the_program(tmp_path):
    (tmp_path / "somewhere" / ".venv").mkdir(parents=True)
    (tmp_path / "somewhere" / ".venv" / "important.txt").write_text("x")
    lines = []
    code = U.run(["--yes"], root=tmp_path / "somewhere", say=lines.append)
    assert code == U.EXIT_FAILED and "不確定這是本程式的資料夾" in "\n".join(lines)
    assert (tmp_path / "somewhere" / ".venv" / "important.txt").exists()


def test_refuses_dangerous_roots(tmp_path, monkeypatch):
    assert U.validate_root(Path(Path.cwd().anchor))  # 磁碟根目錄
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for name in U.MARKER_FILES:  # 就算裡面剛好有標記檔案，使用者資料夾本身也不行
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x")
    assert "使用者資料夾" in U.validate_root(tmp_path)
    assert U.validate_root(tmp_path.parent)  # 使用者資料夾的上層也不行


@pytest.mark.skipif(sys.platform == "win32", reason="用 flock 模擬「程式開著」")
def test_refuses_while_the_app_is_running(env):
    from media_sorter.config import acquire_app_lock

    lock = acquire_app_lock(env["root"] / "logs")  # 跟視窗程式用同一個鎖
    try:
        code, out = run(env, "--yes")
    finally:
        lock.close()
    assert code == U.EXIT_FAILED and "正在執行中" in out
    assert (env["root"] / ".venv" / "install-ok.txt").exists() and user_files_intact(env)
    code, _ = run(env, "--yes")  # 關掉之後就可以了
    assert code == U.EXIT_OK


def test_app_running_check_does_not_create_folders(tmp_path):
    empty = tmp_path / "prog"
    empty.mkdir()
    assert not U.app_is_running(empty) and not (empty / "logs").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="需要建立符號連結")
def test_a_linked_venv_only_loses_the_link(env, tmp_path):
    root = env["root"]
    target = tmp_path / "別的地方的重要資料"
    target.mkdir()
    (target / "重要.txt").write_text("不能刪")
    import shutil

    shutil.rmtree(root / ".venv")
    os.symlink(target, root / ".venv", target_is_directory=True)
    code, _ = run(env, "--yes")
    assert code == U.EXIT_OK and not (root / ".venv").exists() and not (root / ".venv").is_symlink()
    assert (target / "重要.txt").read_text() == "不能刪"


def test_running_from_inside_the_venv_leaves_the_venv_to_the_batch_file(env):
    root = env["root"]
    code, out = run(env, "--yes", "--remove-settings", prefix=str(root / ".venv"))
    assert code == U.EXIT_REMOVE_VENV_LATER  # uninstall.bat 看到 10 才會在這個程式結束後移除 .venv
    assert (root / ".venv" / "install-ok.txt").exists()  # 正在用它，所以現在不動
    assert not (root / "settings.json").exists()  # 其他項目照做
    assert U.running_inside_venv(root, str(root / ".venv" / "Scripts"))
    assert not U.running_inside_venv(root, str(root.parent))


def test_failures_are_reported_honestly(env, monkeypatch):
    real = U._rmtree

    def broken(path):
        if path.name == ".venv":
            raise PermissionError(13, "檔案正在使用中", str(path))
        real(path)

    monkeypatch.setattr(U, "_rmtree", broken)
    code, out = run(env, "--yes", "--remove-settings")
    assert code == U.EXIT_FAILED and "沒有移除成功" in out and "檔案正在使用中" in out
    assert "解除安裝完成" not in out
    assert not (env["root"] / "settings.json").exists()  # 其他項目仍然完成
    assert user_files_intact(env)


def test_targets_are_revalidated_before_deleting(env, tmp_path):
    outside = tmp_path / "無關的檔案.txt"
    outside.write_text("x")
    evil = U.Target("venv", "壞的清單", "", [outside], env["root"])
    problems = U.remove_target(evil)
    assert problems and "拒絕刪除" in problems[0] and outside.exists()
    also = U.Target("venv", "上一層", "", [env["root"] / ".." / "手機照片"], env["root"])
    assert U.remove_target(also) and env["photos"].exists()
    logs_dir = U.Target("logs", "資料夾", "", [env["root"] / "logs" / U.QUARANTINE_DIR], env["root"] / "logs",
                        only_files=True)
    assert U.remove_target(logs_dir) and env["quarantined"].exists()  # logs 只刪檔案，不刪資料夾


def test_model_cache_only_our_two_models_and_only_when_asked(env, tmp_path):
    cache = tmp_path / "hf" / "hub"
    (cache / MODEL_B / "snapshots" / "abc").mkdir(parents=True)
    (cache / MODEL_B / "snapshots" / "abc" / "model.bin").write_text("m")
    (cache / "models--someone--else").mkdir()
    (cache / "models--someone--else" / "keep.bin").write_text("別的程式的模型")
    code, out = run(env, "--yes", cache=cache)  # 沒有指定 → 不刪模型
    assert code == U.EXIT_OK and (cache / MODEL_B).exists()
    code, out = run(env, "--yes", "--remove-model", cache=cache)
    assert code == U.EXIT_OK  # .venv 已經不在，只剩模型
    assert not (cache / MODEL_B).exists()
    assert (cache / "models--someone--else" / "keep.bin").exists()  # 別的程式的模型不會被動到


def test_model_prompt_warns_that_the_cache_is_shared(env, tmp_path):
    cache = tmp_path / "hf" / "hub"
    (cache / MODEL_B).mkdir(parents=True)
    _, out = run(env, "--dry-run", cache=cache)
    assert "可能跟其他程式共用" in out and str(cache) in out


def test_hf_cache_location_follows_the_environment(tmp_path):
    assert U.hf_cache_dir({"HF_HUB_CACHE": "/a"}) == Path("/a")
    assert U.hf_cache_dir({"HF_HOME": "/b"}) == Path("/b/hub")
    assert U.hf_cache_dir({"XDG_CACHE_HOME": "/c"}) == Path("/c/huggingface/hub")
    assert U.hf_cache_dir({}) == Path.home() / ".cache" / "huggingface" / "hub"


def test_nothing_to_remove_is_not_an_error(tmp_path):
    root = tmp_path / "prog"
    for name in U.MARKER_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x")
    lines = []
    code = U.run(["--yes"], root=root, cache_dir=tmp_path / "none", say=lines.append)
    assert code == U.EXIT_OK and "已經解除安裝" in "\n".join(lines)


def test_model_names_match_what_the_program_downloads():
    open_clip = pytest.importorskip("open_clip")
    from media_sorter.config import MODEL_PRESETS

    repos = {"models--" + open_clip.get_pretrained_cfg(p["arch"], p["pretrained"])["hf_hub"].strip("/").replace("/", "--")
             for p in MODEL_PRESETS.values()}
    assert repos == set(U.MODEL_CACHE_DIRS)


def test_readonly_files_can_be_removed(env):
    if sys.platform == "win32":
        pytest.skip("唯讀屬性在 Windows 才會擋刪除")
    victim = env["root"] / ".venv" / "Lib" / "site-packages" / "torch.py"
    victim.chmod(0o444)
    (victim.parent).chmod(0o555)  # 目錄唯讀：刪除需要先改權限
    try:
        code, _ = run(env, "--yes")
    finally:
        if victim.parent.exists():
            victim.parent.chmod(0o755)
    assert code == U.EXIT_OK and not (env["root"] / ".venv").exists()


# ---------------------------------------------------------------------------- uninstall.bat
def test_uninstall_bat_is_safe_and_windows_friendly():
    raw = (ROOT / "uninstall.bat").read_bytes()
    text = raw.decode("utf-8")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert "media_sorter.uninstaller" in text and "%*" in text
    assert 'if "%RC%"=="10"' in text  # 用 .venv 的 Python 執行時，結束後才移除 .venv
    # 批次檔裡唯一的刪除動作就是移除 .venv；沒有萬用字元、沒有 del、沒有 %~dp0 之外的路徑
    deletes = re.findall(r"^\s*(?:rmdir|rd|del|erase|rm)\b.*$", text.replace("\r\n", "\n"), flags=re.M | re.I)
    assert deletes == ['    rmdir /s /q ".venv" >nul 2>&1']
    assert "*" not in "".join(deletes) and "cd /d \"%~dp0\"" in text
    assert "uninstaller.py" in text.split("cd /d")[1]  # 在程式資料夾裡才會執行（找不到就停）


def test_docs_and_readme_describe_uninstall():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    doc = (ROOT / "使用說明與免責聲明_Usage-and-Disclaimer.txt").read_text(encoding="utf-8-sig")
    for text in (readme, doc):
        assert "uninstall.bat" in text and "復原移除" in text
    assert "not modify the registry" in doc or "登錄檔" in doc


# ---------------------------------------------------------------------------- 安裝版（setup.exe）：只處理使用者資料
def make_data_dir(base: Path) -> Path:
    data = base / "LocalAppData" / "AI Media Sorter"
    (data / "logs" / U.QUARANTINE_DIR / "x").mkdir(parents=True)
    (data / U.DATA_MARKER).write_text("marker")
    (data / "settings.json").write_text("{}")
    (data / "categories.json").write_text("{}")
    (data / "logs" / "整理紀錄_20240101_000000.csv").write_text("log")
    (data / "logs" / "error.log").write_text("e")
    (data / "logs" / U.QUARANTINE_DIR / "x" / "複本.jpg").write_text("你的檔案")
    return data


def run_data(env, data, *args, cache=None):
    lines = []
    code = U.run(["--data-only", "--data-dir", str(data), *args], root=env["root"],
                 cache_dir=cache or env["root"].parent / "hf-none", say=lines.append)
    return code, "\n".join(lines)


def test_data_only_removes_just_the_ticked_items(env, tmp_path):
    data = make_data_dir(tmp_path)
    code, _ = run_data(env, data, "--yes")  # 什麼都沒勾 → 什麼都不刪
    assert code == U.EXIT_OK and (data / "settings.json").exists() and (data / "logs" / "error.log").exists()
    code, _ = run_data(env, data, "--yes", "--remove-settings")
    assert code == U.EXIT_OK
    assert not (data / "settings.json").exists() and (data / "logs" / "error.log").exists()
    code, _ = run_data(env, data, "--yes", "--remove-logs")
    assert code == U.EXIT_OK and not (data / "logs" / "error.log").exists()
    assert (data / "logs" / U.QUARANTINE_DIR / "x" / "複本.jpg").read_text() == "你的檔案"  # 使用者的檔案永遠保留
    assert (data / U.DATA_MARKER).exists()  # 還有東西，標記檔與資料夾都留著
    assert user_files_intact(env)


def test_data_dir_is_removed_only_when_nothing_else_is_left(env, tmp_path):
    data = tmp_path / "LocalAppData" / "AI Media Sorter"
    (data / "logs").mkdir(parents=True)
    (data / U.DATA_MARKER).write_text("m")
    (data / "settings.json").write_text("{}")
    (data / "logs" / "error.log").write_text("e")
    code, _ = run_data(env, data, "--yes", "--remove-settings", "--remove-logs")
    assert code == U.EXIT_OK and not data.exists()


def test_data_only_refuses_directories_that_are_not_ours(env, tmp_path, monkeypatch):
    stranger = tmp_path / "Documents"
    stranger.mkdir()
    (stranger / "settings.json").write_text("別人的設定")
    code, out = run_data(env, stranger, "--yes", "--remove-settings")
    assert code == U.EXIT_FAILED and "沒有本程式的標記檔" in out and (stranger / "settings.json").exists()
    assert U.validate_data_dir(tmp_path / "不存在")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / U.DATA_MARKER).write_text("m")
    assert "不是本程式的資料夾" in U.validate_data_dir(tmp_path)  # 使用者資料夾本身就算有標記檔也不行


@pytest.mark.skipif(sys.platform == "win32", reason="用 flock 模擬「程式開著」")
def test_data_only_check_running_and_active_logs(env, tmp_path):
    from media_sorter.config import acquire_app_lock

    data = make_data_dir(tmp_path)
    assert U.run(["--check-running", "--data-dir", str(data)], root=env["root"], say=lambda s: None) == U.EXIT_OK
    lock = acquire_app_lock(data / "logs")
    try:
        assert U.run(["--check-running", "--data-dir", str(data)], root=env["root"],
                     say=lambda s: None) == U.EXIT_RUNNING
        code, out = run_data(env, data, "--yes", "--remove-settings")
        assert code == U.EXIT_FAILED and "正在執行中" in out and (data / "settings.json").exists()
    finally:
        lock.close()
    assert U.run(["--active-logs", "--data-dir", str(data)], root=env["root"], say=lambda s: None) == 1


def test_model_removal_in_data_mode(env, tmp_path):
    data = make_data_dir(tmp_path)
    cache = tmp_path / "hf" / "hub"
    (cache / MODEL_B).mkdir(parents=True)
    (cache / MODEL_B / "m.bin").write_text("m")
    code, _ = run_data(env, data, "--yes", "--remove-model", cache=cache)
    assert code == U.EXIT_OK and not (cache / MODEL_B).exists() and (data / "settings.json").exists()


def test_bat_uninstaller_refuses_an_installed_version(env):
    (env["root"] / "installed.flag").write_text("x")
    code, out = run(env, "--yes")
    assert code == U.EXIT_FAILED and "安裝程式安裝的版本" in out
    assert (env["root"] / ".venv" / "install-ok.txt").exists() and user_files_intact(env)
