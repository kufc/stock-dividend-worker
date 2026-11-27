# 上市前檢查清單

程式這一邊能在開發環境完成的都做了。下面是**只有你（發行者）能做**的事，以及每一項為什麼卡住上市。
開發環境沒有真正的 Windows、沒有顯示卡、沒有簽章憑證；Windows 的行為是用 Wine 加安裝版內建的 Windows Python 驗證的。

依重要性排列。前三項沒完成之前，不建議公開發行。

## 1. 在真的 Windows 上驗證（必做）

在一台乾淨的 Windows 10 與一台 Windows 11 上（至少一台有 NVIDIA 顯示卡）：

- [ ] 執行安裝程式，走完精靈；確認 PyTorch 與模型下載完成、開始功能表有捷徑
      （這一步會實際安裝 `media_sorter/installer.py` 裡固定的 CUDA 版 PyTorch——那些數 GB 的檔案在開發環境沒有下載過，
      只核對過下載站索引頁上的雜湊）
- [ ] 用安裝好的 Python 跑測試：`python -m pytest`（Windows 專屬的測試在這裡才不會被略過）
- [ ] 設定 `MEDIA_SORTER_TEST_REAL_TRASH=1` 再跑 `tests/test_regressions_review8.py`：
      確認**真的資源回收筒**能在程式握著保護時搬走檔案（Wine 驗不到這一點，這是檔案安全最關鍵的一步）
- [ ] 用幾百張自己的照片實際走一遍：辨識 → 檢查 → 複製 → 「將這批原檔移到回收筒」→ 從回收筒還原一個檔案
- [ ] 來源放在**網路磁碟**與**隨身碟**各試一次「將這批原檔移到回收筒」：應該拒絕並說明原因
- [ ] 在回收筒的內容裡選「不要將檔案移到資源回收筒」再試一次；另外把回收筒容量設成很小、移除一個比它大的檔案：
      兩種都應該拒絕並說明原因（程式讀的是登錄檔裡的設定，這只在 Wine 裡驗證過；
      沒改過設定時的預設容量是用公式估的，請用一個很大的影片實測一次）
- [ ] **長路徑**：把一個檔案放在總長超過 240 個字元的路徑裡，移除原檔應該拒絕；另外試一個 200～240 個字元的，
      確認真的進了回收筒而不是被永久刪除（240 這個界線是保守估的，沒有實測）
- [ ] **「圖片」資料夾由 OneDrive 接管**時整理並移除原檔：確認檔案進了本機的回收筒；
      再試「圖片」資料夾被重新導向到另一顆磁碟或網路位置（程式會讀 `BitBucket\KnownFolder` 的設定，只在 Wine 裡驗證過）
- [ ] **驗證並開啟新的回收筒介面**（`media_sorter/recycle.py`：IFileOperation 加「不能進回收筒就失敗」的旗標，
      讓 Windows 自己把關，不必只靠我們列舉的規則）。程式已經寫好但**預設關閉**，因為 Wine 的這個介面是空殼，
      真正搬檔案的呼叫從來沒有執行過。在真的 Windows 上：
      0. `python tools/verify_recycle_windows.py`：應該印出「通過：檔案進了資源回收筒」（它會核對回收筒的項目數多了一個）
      1. `set MEDIA_SORTER_MODERN_RECYCLE=1` 後執行 `python -m pytest tests/test_recycle.py -s`：
         `test_real_call_either_recycles_or_…` 必須通過，而且**不能**印出「IFileOperation 不支援」；到回收筒確認 `recycle-me.jpg` 在裡面
      2. 同一個環境變數下執行 `python -m pytest tests/test_regressions_review7.py tests/test_regressions_review8.py tests/test_regressions_review9.py tests/test_copy_first_safety.py`
      3. 開著環境變數實際用程式移除一批原檔，再從回收筒還原
      4. 把回收筒設成「不要將檔案移到資源回收筒」、另外把容量設得比檔案小，各試一次：應該失敗並保留原檔
         （先暫時讓 `organizer.recycle_bin_problem` 放行才測得到這一層）
      5. 全部通過後把 `recycle.MODERN_DEFAULT` 改成 `True`；任何一步不對就維持關閉
- [ ] 免安裝版：在只有 Python 3.13 的電腦上執行 `install.bat`，應該明確要求 3.12 而不是裝下去
- [ ] 把 `settings.json` 改成空檔再開程式：應該能開，並提示已改用預設值
- [ ] 辨識到一半用工作管理員結束程式，重開後確認接續、改過的分類還在
- [ ] 拔掉網路線開程式辨識：應該正常；把模型資料夾改名後再試：應該詢問要不要下載，而不是自己連網
- [ ] 解除安裝（預設選項）後檢查殘留；再裝一次，勾選所有移除選項解除安裝，確認
      `%LOCALAPPDATA%\AI Media Sorter` 與 `%USERPROFILE%\.cache\huggingface\hub` 裡本程式的模型都清掉
      （含 `hub\blobs` 裡的大檔案；Windows 上 Hugging Face 的檔案配置跟 Linux 不一定相同，這一點沒辦法在這裡確認）
- [ ] 至少一台舊顯示卡（GTX 10 系列）與一台沒有顯示卡的電腦

## 2. 程式碼簽章（必做）

沒有簽章時，Windows SmartScreen 會警告每一位使用者，而且他們無法確認安裝檔沒有被改過。

- [ ] 取得程式碼簽章憑證（OV 即可；EV 可以立刻消除 SmartScreen 警告，OV 需要累積下載量）
- [ ] 依 README「數位簽章」一節設定環境變數，用 `python installer/build_installer.py --publisher "名稱" --sign` 建置
- [ ] 簽章後把 README 與使用說明裡「安裝程式沒有數位簽章」的段落改掉
      （`tests/test_pinned_versions.py` 目前會檢查那段文字存在，簽章後要一起更新）
- [ ] 在發行頁面公布安裝檔與 ZIP 的 SHA-256

## 3. 法律與授權（必做，需要專業意見）

- [ ] 請律師看過「使用說明與免責聲明」：它不是法律意見，責任限制條款在你的銷售地區是否有效需要確認
- [ ] 確認可以商用的授權：模型（LAION 的 CLIP 權重、xlm-roberta）、PyTorch、OpenCLIP、transformers、
      FFmpeg（透過 PyAV；注意 LGPL／GPL 的組態）、pillow-heif（HEIC 的專利問題）、conda-forge 的 Python
- [ ] 模型是用網路資料訓練的：確認你對「可能有偏誤的分類結果」的說明足夠
- [ ] 隱私權說明：程式不上傳照片，但安裝與下載模型時會連到 PyPI、PyTorch、Hugging Face

## 4. 發行流程

- [ ] 版本號（`media_sorter/__init__.py`）與使用說明裡的版本一致（測試會檢查）
- [ ] `python tools/build_zip.py` 產生 ZIP（工作目錄要乾淨；ZIP 註解裡有 commit 編號）
- [ ] 產生相依成分清單（SBOM）：`requirements.lock` 加上安裝檔裡的 `RUNTIME-PACKAGES.txt` 是完整來源，
      轉成 CycloneDX 的工具還沒寫
- [ ] 決定更新方式：目前是「下載新的安裝程式再裝一次」（設定與辨識結果會保留）
- [ ] 支援管道與問題回報方式（程式的錯誤視窗會請使用者回報，但沒有寫回報到哪裡）

## 5. 已知限制（發行說明裡應該寫）

- 資料夾裡不屬於任何分類的東西，大約三成仍會被分進某個分類（`docs/evaluation.md`）
- 只支援 Windows 10／11 64 位元。程式在其他系統上跑得起來，但「留下的那一份不能被刪除」的保護只有 Windows 有
- 影片只取樣少數畫面，沒有影片的準確度數字
- 沒有重複檔偵測；Live Photo、RAW＋JPEG、sidecar 不會被當成一組
- 介面只有繁體中文
- 整台電腦突然斷電時，最後幾個檔案的辨識結果可能要重算

## 6. 開發環境裡還沒解決的一件事

用 Wine 跑 `tests/test_workdb.py` 整個檔案時，排在「重開程式」測試後面的兩個介面測試會在建立視窗時失敗
（Tk 讀不到自己的啟動檔）；單獨跑會過，Linux 全過。看起來是 Wine 裡同一個行程反覆建立視窗的問題，不是程式的問題，
但沒有證明。第 1 項在真的 Windows 上跑測試時請留意這兩個測試。
