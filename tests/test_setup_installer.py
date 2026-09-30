"""Windows 安裝程式（setup.exe）的建置腳本檢查。實際「安裝→解除安裝」的行為驗證見 installer/test_in_wine.sh。"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NSI = (ROOT / "installer" / "setup.nsi").read_text(encoding="utf-8-sig")


def test_installs_per_user_and_only_touches_hkcu():
    assert "RequestExecutionLevel user" in NSI and "Target amd64-unicode" in NSI
    assert not re.search(r"\bHKLM\b|\bHKCR\b|SetShellVarContext all", NSI)  # 不需要系統管理員權限，也不寫系統範圍的登錄檔
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\${APPDIR}"' in NSI


def test_appears_in_windows_apps_list():
    for key in ("DisplayName", "DisplayVersion", "Publisher", "DisplayIcon", "InstallLocation", "UninstallString",
                "QuietUninstallString", "EstimatedSize", "NoModify"):
        assert f'"{key}"' in NSI, key
    assert "DeleteRegKey HKCU \"${UNINST_KEY}\"" in NSI and "DeleteRegKey HKCU \"${APP_KEY}\"" in NSI  # 解除安裝時清乾淨


def test_install_folder_is_always_our_own_subfolder():
    normalize = NSI[NSI.index("Function NormalizeInstDir"):NSI.index("Function DirLeave")]
    assert '"$INSTDIR\\${APPDIR}"' in normalize and "GetFileName" in normalize
    assert "Call NormalizeInstDir" in NSI.split("Function .onInit")[1].split("SectionEnd")[0]  # 靜默安裝 /D= 也會處理
    assert "Call NormalizeInstDir" in NSI.split("Function DirLeave")[1].split("FunctionEnd")[0]


def test_uninstall_only_deletes_what_setup_put_there():
    section = NSI[NSI.index('Section "Uninstall"'):]
    recursive = re.findall(r"RMDir /r (\S+)", section)
    assert recursive == ['"$INSTDIR\\python"', '"$INSTDIR\\app"']  # 唯二會遞迴刪除的位置
    assert re.findall(r'^\s*RMDir "\$INSTDIR"\s*$', section, flags=re.M)  # 安裝資料夾本身是非遞迴（不是空的就保留）
    assert "RMDir /r \"$INSTDIR\"" not in NSI and "RMDir /r $INSTDIR" not in NSI
    assert not re.search(r"RMDir /r .*(DataDir|LOCALAPPDATA|DESKTOP|SMPROGRAMS|PROFILE)", NSI)  # 使用者資料不會被安裝程式直接遞迴刪除
    init = NSI[NSI.index("Function un.onInit"):NSI.index("Function un.DataPageCreate")]
    assert 'installed.flag' in init and "Abort" in init  # 不是安裝出來的資料夾就拒絕


def test_user_data_is_kept_unless_ticked_and_goes_through_the_safe_uninstaller():
    section = NSI[NSI.index('Section "Uninstall"'):]
    assert "media_sorter.uninstaller --data-only" in section
    assert 'StrCpy $UnRmSettings "0"' in NSI and 'StrCpy $UnRmLogs "0"' in NSI and 'StrCpy $UnRmModel "0"' in NSI  # 預設保留
    assert "/REMOVE_SETTINGS" in NSI and "/REMOVE_LOGS" in NSI and "/REMOVE_MODEL" in NSI  # 靜默解除安裝也可指定


def test_refuses_to_run_while_the_program_is_open():
    assert NSI.count("--check-running") >= 2 and "$0 == 42" in NSI  # 安裝（升級）與解除安裝都會檢查


def test_license_must_be_accepted_and_deps_step_is_retryable():
    assert "MUI_LICENSEPAGE_CHECKBOX" in NSI and "MUI_PAGE_LICENSE" in NSI
    assert "MB_RETRYCANCEL" in NSI and "IDRETRY deps" in NSI and "/SKIPDEPS" in NSI
    assert "setup-deps.cmd" in NSI


def test_cmd_helper_is_windows_friendly():
    raw = (ROOT / "installer" / "setup-deps.cmd").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    text = raw.decode("utf-8")
    assert "media_sorter.installer" in text and "..\\python\\python.exe" in text
    assert re.findall(r"^\s*(?:del|rmdir|rd|erase)\b", text, flags=re.M | re.I) == []


def test_icon_exists():
    assert (ROOT / "installer" / "app.ico").stat().st_size > 1000


@pytest.mark.skipif(not shutil.which("makensis"), reason="需要 NSIS（apt install nsis）")
def test_script_compiles_and_stages_the_right_files(tmp_path):
    runtime = tmp_path / "runtime"
    for name in ("python.exe", "pythonw.exe", "python312.dll"):
        (runtime / name).parent.mkdir(parents=True, exist_ok=True)
        (runtime / name).write_bytes(b"MZ fake")
    (runtime / "Lib" / "site-packages" / "x" / "tests").mkdir(parents=True)
    (runtime / "Lib" / "site-packages" / "x" / "tests" / "t.py").write_text("t")
    (runtime / "python.pdb").write_bytes(b"pdb")
    out = tmp_path / "out"
    subprocess.run([sys.executable, str(ROOT / "installer" / "build_installer.py"), "--runtime", str(runtime),
                    "--out", str(out)], check=True, capture_output=True)
    exes = list(out.glob("AI-Media-Sorter-Setup-*.exe"))
    assert len(exes) == 1 and exes[0].stat().st_size > 100_000
    stage = out / "stage"
    assert (stage / "app" / "installed.flag").exists() and (stage / "app" / "media_sorter" / "uninstaller.py").exists()
    assert (stage / "app" / "setup-deps.cmd").exists() and (stage / "license.txt").exists()
    assert not (stage / "app" / "tests").exists() and not list((stage / "app").rglob("__pycache__"))
    assert (stage / "python" / "python.exe").exists() and not (stage / "python" / "python.pdb").exists()
    assert not (stage / "python" / "Lib" / "site-packages" / "x" / "tests").exists()  # 精簡掉不需要的東西
    from media_sorter import __version__

    assert exes[0].name == f"AI-Media-Sorter-Setup-{__version__}.exe"


def test_running_program_blocks_silent_uninstall_with_exit_code_5():
    section = NSI[NSI.index('Section "Uninstall"'):]
    assert section.split("\n")[1].strip() == "Call un.CheckNotRunning"  # 區段一開始就檢查（靜默模式在這裡才帶得出代碼）
    func = NSI[NSI.index("Function un.CheckNotRunning"):NSI.index("Function un.DataPageCreate")]
    assert "SetErrorLevel 5" in func and "Abort" in func


def test_disk_space_includes_the_downloads_and_upgrade_cleans_old_code():
    assert re.search(r"AddSize \d{7,}", NSI)  # 「所需空間」要算進數 GB 的下載
    main = NSI[NSI.index('Section "$(SEC_MAIN)"'):NSI.index("SectionEnd")]
    assert main.index('RMDir /r "$INSTDIR\\app\\media_sorter"') < main.index('File /r "${STAGE}\\app\\*.*"')


def test_finish_page_only_offers_to_run_when_components_are_ready():
    show = NSI[NSI.index("Function FinishShow"):NSI.index("Function LaunchApp")]
    assert "install-ok.txt" in show and "SW_HIDE" in show and "FIN_NODEPS" in show
    launch = NSI[NSI.index("Function LaunchApp"):]
    assert "install-ok.txt" in launch.split("FunctionEnd")[0]


def test_wizard_uses_our_own_artwork_and_no_tofu_symbols():
    for name in ("wizard.bmp", "header.bmp"):
        assert name in NSI and (ROOT / "installer" / name).stat().st_size > 1000
    assert "⚠" not in NSI  # 部分 Windows 字型沒有這個符號，會顯示成方框


def test_optional_code_signing_signs_setup_and_uninstaller():
    assert "!finalize '${SIGN_CMD}'" in NSI and "!uninstfinalize '${SIGN_CMD}'" in NSI


def test_sign_helper_builds_commands_without_leaking_the_password(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "installer"))
    import sign

    pfx = tmp_path / "c.pfx"
    pfx.write_bytes(b"x")
    monkeypatch.setattr(sign.shutil, "which", lambda name: "/usr/bin/" + name if name == "osslsigncode" else None)
    cmd, out = sign.build_command(tmp_path / "a.exe", {"MEDIA_SORTER_SIGN_PFX": str(pfx),
                                                      "MEDIA_SORTER_SIGN_PASSWORD": "pw"})
    assert cmd[:2] == ["/usr/bin/osslsigncode", "sign"] and "-ts" in cmd and out.name == "a.exe.signed"
    cmd, _ = sign.build_command(tmp_path / "a.exe", {"MEDIA_SORTER_SIGN_PFX": str(pfx),
                                                    "MEDIA_SORTER_SIGN_TIMESTAMP": ""})
    assert "-ts" not in cmd and "-pass" not in cmd
    with pytest.raises(SystemExit):
        sign.build_command(tmp_path / "a.exe", {"MEDIA_SORTER_SIGN_PFX": str(tmp_path / "missing.pfx")})


@pytest.mark.skipif(not (shutil.which("osslsigncode") and shutil.which("openssl")), reason="需要 osslsigncode 與 openssl")
def test_sign_helper_reports_failures(tmp_path):
    sys.path.insert(0, str(ROOT / "installer"))
    import sign

    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(tmp_path / "k.pem"), "-out",
                    str(tmp_path / "c.pem"), "-days", "2", "-nodes", "-subj", "/CN=Test"], check=True, capture_output=True)
    subprocess.run(["openssl", "pkcs12", "-export", "-out", str(tmp_path / "t.pfx"), "-inkey", str(tmp_path / "k.pem"),
                    "-in", str(tmp_path / "c.pem"), "-passout", "pass:pw"], check=True, capture_output=True)
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"not a PE file")  # 不是執行檔：簽章一定失敗，要回報錯誤而不是假裝成功
    with pytest.raises(SystemExit) as info:
        sign.sign(exe, {"MEDIA_SORTER_SIGN_PFX": str(tmp_path / "t.pfx"), "MEDIA_SORTER_SIGN_PASSWORD": "pw",
                        "MEDIA_SORTER_SIGN_TIMESTAMP": ""})
    assert "簽章失敗" in str(info.value) and " pw" not in str(info.value)
