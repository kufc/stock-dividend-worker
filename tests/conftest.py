import os
import sys
import tempfile
from pathlib import Path

# 測試時把設定檔、紀錄寫到暫存資料夾，不影響使用者的資料
os.environ.setdefault("MEDIA_SORTER_HOME", tempfile.mkdtemp(prefix="media_sorter_test_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
