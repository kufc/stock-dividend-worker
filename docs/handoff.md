# 交接說明（給接手的人或 AI）

這份文件讓第一次看到這個專案的人，不用讀完整段開發歷史就能接手。先讀這份，再依需要看後面列的文件。

## 這是什麼

**AI 媒體分類器（AI Media Sorter）**：Windows 桌面程式。掃描資料夾裡的照片與影片，用本機的多語言 CLIP 模型
（OpenCLIP，LAION 的 xlm-roberta 版本）判斷每個檔案最像使用者設定的哪個分類，讓使用者檢查、修改後，
**複製**到分類資料夾。介面是繁體中文的 tkinter，版本 1.1.0。

產品底線（任何修改都不能削弱）：

1. **只複製、原檔不動。**「移除原檔」是另外的、可選的、送資源回收筒的動作，而且移除前一定要有另一份
   內容逐位元組相同的檔案存在（`media_sorter/organizer.py`）。
2. **辨識時嚴格離線。** 只有使用者明確同意下載或要求修復模型時才連網（`media_sorter/netpolicy.py`）。
3. **照片不離開電腦。**

## 目前狀態

- 程式碼在 git 分支 `claude/relaxed-johnson-02ap4m`（repo：kufc/stock-dividend-worker）。
- 已完成一次外部審查的 P0 與全部 P1，並依「朝可以上市的方向」做了產品決定（見下面）。
- 測試：一般環境 380 passed／34 skipped；裝了 torch 與真模型的環境 397 passed／17 skipped。
  略過的是需要 torch、真正 Windows 或簽章工具的測試。
- **還不能公開發行**：缺真正 Windows 上的驗證、程式碼簽章、授權的法律確認。清單在 `docs/release-checklist.md`。

## 檔案地圖

| 路徑 | 內容 |
| --- | --- |
| `media_sorter/app.py` | 視窗程式：四個步驟（選資料夾 → 檢查分類 → 預覽 → 完成）、背景工作 |
| `media_sorter/views.py`、`dialogs.py`、`grid.py`、`theme.py` | 版面、對話框、縮圖格、外觀 |
| `media_sorter/analysis.py` | 辨識流程、`Item`（每個檔案的狀態）、「需檢查」規則 `Item.is_low` |
| `media_sorter/classifier.py` | 載入模型、計分（softmax；分類少於 3 個時跟「其他東西」一起比） |
| `media_sorter/media.py` | 讀圖、影片取樣、縮圖；3 億像素硬上限 `check_pixels` |
| `media_sorter/organizer.py` | 複製（fsync＋讀回驗證）、整理紀錄、復原、移除原檔、檔案鎖、回收筒偵測 |
| `media_sorter/netpolicy.py` | 離線政策、模型檔核對（固定 commit＋SHA-256）、下載與修復 |
| `media_sorter/workdb.py` | SQLite 工作資料庫與縮圖磁碟快取（可接續、只重算變更的檔案） |
| `media_sorter/config.py` | 預設設定、模型清單與固定版本、使用者資料位置 |
| `media_sorter/installer.py`、`uninstaller.py` | 安裝 PyTorch 與套件（固定版本＋雜湊）、安全的解除安裝 |
| `installer/` | NSIS 安裝程式、建置腳本、Wine 測試腳本、簽章腳本 |
| `tests/` | 自動測試；`tests/manual/` 是評測資料（真模型的向量檔與來源清單，圖片本身不在 repo） |
| `tools/` | `evaluate.py`（重算評測）、`build_zip.py`（打包）、`update_lock.py`、`update_model_pins.py` |
| `docs/evaluation.md` | 準確度評測：資料、方法、結果、限制 |
| `docs/roadmap.md` | 沒做的功能與已經做的產品決定 |
| `docs/release-checklist.md` | 上市前只有發行者能做的事 |
| `README.md` | 使用者與開發者說明 |
| `使用說明與免責聲明_Usage-and-Disclaimer.txt` | 中英對照的使用說明（`tests/test_docs.py` 會檢查跟程式用語一致） |

## 準確度（獨立測試集，1,176 張）

| | 標準模型 | 高精準模型 |
| --- | ---: | ---: |
| 檔案屬於某個預設分類時，第一名分對（各分類平均） | 97.5% | 96.8% |
| 不屬於任何分類、卻沒被標成「需檢查」（門檻 70） | 30.4% | 32.3% |
| 「不進需檢查的部分」分錯的比例（測試集 36% 是未知內容） | 16.3% | 17.2% |

- 最大的弱點：資料夾裡混了不屬於任何分類的東西時，大約三成會被分進某個分類。
- 門檻 70 是看過這份測試集的結果後才決定調高的（數值依開發集選），所以後兩列可能略為樂觀。
  **下一次要調描述、規則或門檻之前，要先收集一份新的獨立測試集**，不准用現有這份調任何東西。
- 細節：`docs/evaluation.md`。不需要模型就能重算：`python tools/evaluate.py`。

## 已經做的產品決定

- 定位：「整理完就結束」的工具，這一版不做媒體庫（搜尋、多標籤）。
- 「需檢查」門檻 70；沒把握的檔案預設放「未分類」資料夾。
- 「自動」一律用標準模型（高精準沒有比較準，還要多下載 3.5 GB）。
- 只支援 Windows 10／11 64 位元。
- 原檔在網路磁碟、隨身碟（沒有回收筒）時不移除，並說明原因。

## 怎麼跑起來

```bash
# Linux 開發環境（測試需要 tkinter）
apt-get install -y python3-tk xvfb
python3.12 -m venv .venv --system-site-packages
.venv/bin/pip install pytest "numpy<3" "Pillow<13" "av<19" "send2trash<2" pillow-heif
xvfb-run -a .venv/bin/python -m pytest -q -p no:cacheprovider

# 需要真模型的測試：另外安裝 CPU 版 torch 與 requirements.txt
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# Windows 上：雙擊 install.bat 再雙擊 start.bat（免安裝版），或建置安裝程式
python installer/build_installer.py --out dist     # 需要 NSIS 與 pip install py-rattler
```

在 Linux 上驗證 Windows 行為：Wine＋安裝版內建的 Windows Python，做法寫在 `installer/test_in_wine.sh` 開頭。
注意：Ubuntu 的 Wine 9 沒有 `/run/user/<uid>` 會一啟動就當掉；Wine 裡 numpy 要用 1.26.4。

## 工作守則（之前的開發一直遵守）

- 先讀程式與測試、重現問題，再改。每個改動分開 commit，說明「原本的行為、改成什麼、怎麼驗證」。
- 推送前整套測試要綠。**不要為了讓測試通過而刪測試、放寬斷言或加 skip**；真的要改斷言時在 commit 說明原因。
- 行為改了，`README.md` 與使用說明要一起改（中英對照，Windows 換行、UTF-8 BOM）。
- 如實回報：哪些在真 Windows、Wine、Linux 驗證過，哪些沒有。

## 已知的未解問題

- 用 Wine 跑 `tests/test_workdb.py` 整檔時，最後兩個介面測試在建立視窗時失敗（Tk 讀不到 init.tcl），
  單獨跑會過，Linux 全過，原因不明。
- `tests/test_ui_v2.py` 的兩個測試（窄視窗、滑鼠滾輪）在 Wine 下失敗，在這幾輪修改之前就是如此。
- 解除安裝時清理 Hugging Face 共用檔案倉庫（`hub/blobs`）的邏輯，只在 Linux 的檔案配置上驗證過。
- GPU 版 PyTorch 的固定版本只核對過下載站上的雜湊，沒有實際安裝執行過。

## 建議的下一步

1. 依 `docs/release-checklist.md` 在真正的 Windows 上驗證（最關鍵：真的資源回收筒能在程式握著檔案保護時搬走檔案）。
2. 降低未知內容被接受的比例（先在開發集試，再收一份新的獨立測試集驗證）。
3. 完全相同檔案的偵測與 Live Photo／RAW＋JPEG 配對（設計在 `docs/roadmap.md` A3）。
