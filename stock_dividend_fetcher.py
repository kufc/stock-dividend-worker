import os
import requests
import json
from datetime import datetime
import pandas as pd
import time
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

class StockDataFetcher:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        # 初始化 Google Sheets API
        creds_json = os.getenv('GOOGLE_SHEETS_CREDS')
        if not creds_json:
            raise ValueError("找不到 Google Sheets 憑證")
        
        creds_dict = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        self.sheets_service = build('sheets', 'v4', credentials=credentials)
        self.spreadsheet_id = os.getenv('SPREADSHEET_ID')
        if not self.spreadsheet_id:
            raise ValueError("找不到試算表 ID")

    def fetch_stock_data(self, symbol):
        try:
            url = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
            response = requests.get(url, headers=self.headers)
            data = response.json()

            if 'chart' not in data or 'result' not in data['chart'] or not data['chart']['result']:
                print(f"無法取得 {symbol} 的資料")
                return None

            result = data['chart']['result'][0]
            
            # 取得收盤價
            close_price = result['meta'].get('regularMarketPrice', None)
            
            # 取得股息資料
            url_dividend = f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1mo&events=div'
            response_dividend = requests.get(url_dividend, headers=self.headers)
            data_dividend = response_dividend.json()

            dividends = {}
            current_year = datetime.now().year
            for year in range(current_year, current_year-5, -1):
                dividends[str(year)] = 0.0

            if 'chart' in data_dividend and 'result' in data_dividend['chart'] and data_dividend['chart']['result']:
                events = data_dividend['chart']['result'][0].get('events', {})
                if 'dividends' in events:
                    for timestamp, div_data in events['dividends'].items():
                        div_year = datetime.fromtimestamp(int(timestamp)).year
                        if str(div_year) in dividends:
                            dividends[str(div_year)] += div_data['amount']

            return {
                'symbol': symbol,
                'close_price': close_price,
                **dividends
            }
        except Exception as e:
            print(f"處理 {symbol} 時發生錯誤: {str(e)}")
            return None

    def read_stock_list(self):
        try:
            result = self.sheets_service.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range='美股!A2:A'
            ).execute()
            values = result.get('values', [])
            return [row[0] for row in values if row]
        except Exception as e:
            print(f"讀取股票清單時發生錯誤: {str(e)}")
            return []

    def update_sheet_data(self, data_df):
        try:
            # 準備更新的資料
            headers = ['代號', '收盤價', '2024', '2023', '2022', '2021', '2020']
            values = [headers]
            for _, row in data_df.iterrows():
                values.append([
                    row['symbol'],
                    row['close_price'],
                    row['2024'],
                    row['2023'],
                    row['2022'],
                    row['2021'],
                    row['2020']
                ])

            # 更新 Google Sheets
            body = {
                'values': values
            }
            self.sheets_service.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range='美股!A1',
                valueInputOption='RAW',
                body=body
            ).execute()
            print("已成功更新 Google Sheets")
        except Exception as e:
            print(f"更新試算表時發生錯誤: {str(e)}")

    def process_stock_list(self):
        stock_list = self.read_stock_list()
        if not stock_list:
            print("找不到股票清單")
            return

        results = []
        for symbol in stock_list:
            print(f"正在處理 {symbol}")
            stock_data = self.fetch_stock_data(symbol)
            if stock_data:
                results.append(stock_data)

        if results:
            df = pd.DataFrame(results)
            print("\n處理結果：")
            print(df)
            self.update_sheet_data(df)
        else:
            print("沒有可用的資料")

if __name__ == "__main__":
    fetcher = StockDataFetcher()
    fetcher.process_stock_list() 