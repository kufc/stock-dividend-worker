# 股票股息資料擷取器（雲端版）

這個程式可以從 Yahoo Finance 擷取股票資料並將結果儲存到 Google Sheets。

## 功能

- 從 Google Sheets 讀取股票清單
- 擷取每支股票的收盤價和股息資料
- 將結果自動更新到 Google Sheets

## 安裝需求

1. Python 3.8 或更新版本
2. pip（Python 套件管理器）

## 安裝步驟

1. 安裝必要的 Python 套件：
   ```bash
   pip install -r requirements.txt
   ```

2. 設置 Google Cloud Project 和 Google Sheets API：
   - 建立 Google Cloud Project
   - 啟用 Google Sheets API
   - 建立服務帳號並下載憑證

3. 設置環境變數：
   ```powershell
   $env:GOOGLE_SHEETS_CREDS = Get-Content -Path "path/to/your/credentials.json" -Raw
   $env:SPREADSHEET_ID = "your-spreadsheet-id"
   ```

## 使用方法

1. 準備 Google Sheets：
   - 建立新的試算表
   - 建立名為「美股」的工作表
   - 加入必要的欄位標題
   - 將服務帳號加入共用權限

2. 執行程式：
   ```bash
   python stock_dividend_fetcher.py
   ```

## 注意事項

- 確保服務帳號有適當的權限存取 Google Sheets
- 避免過於頻繁的 API 請求以防止被限制
- 定期檢查 Google Cloud Console 的配額使用情況

## 錯誤排除

如果遇到問題：
1. 檢查環境變數是否正確設置
2. 確認服務帳號憑證是否有效
3. 檢查 Google Sheets 的權限設置
4. 查看程式的錯誤訊息和日誌 