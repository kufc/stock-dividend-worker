"""內建的分類描述：預設分類，以及幾個聊天資料夾常見的分類。

每個分類用很多句不同說法一起描述（中文、英文、大範圍的說法與幾種例子），判斷時取平均。
用 1,243 張有標記的真實圖片實測（tests/manual/embeddings_*.npz、tests/test_real_embeddings.py）：
比起每類只有兩三句的舊寫法，12 個預設分類的平均正確率從 95.6% 提高到 97.3%，分對卻被標成「需檢查」的也變少。
逐類精挑細選某幾句並沒有比較好（換一半照片驗證就沒有差別），所以整組一起用。
"""

from __future__ import annotations

from .vocabulary import category_hint

DEFAULT_CATEGORY_NAMES = ["人像", "合照", "貓", "狗", "風景", "城市建築", "美食", "花草植物", "交通工具", "螢幕截圖", "文件收據",
                          "動漫插畫", "迷因梗圖"]

BUILTIN_PROMPTS: dict[str, list[str]] = {
    '人像': [
        "人物照片", "自拍", "a photo of a person", "a selfie", "一個人的照片", "單人照", "人的臉部特寫", "半身照或全身照", "一個人的獨照",
        "a portrait of one person", "a close-up photo of a person's face", "a photo of a single person",
    ],
    '合照': [
        "一群人的合照", "a group photo of several people", "多人合照", "團體照", "兩個人以上的合照", "朋友或家人的合照", "好幾個人一起拍的照片",
        "a group of people posing together", "a photo of two or more people together", "a family photo",
        "a photo of friends together",
    ],
    '貓': [
        "貓咪", "a photo of a cat", "一隻貓", "寵物貓", "小貓", "a kitten", "a pet cat",
    ],
    '狗': [
        "狗狗", "a photo of a dog", "一隻狗", "寵物狗", "小狗", "a puppy", "a pet dog",
    ],
    '風景': [
        "自然風景", "山、海、天空", "a landscape photo of mountains, sea, sky or nature", "戶外風景照", "大自然的景色", "山景或海景",
        "風景照", "a scenic view of nature", "natural scenery", "a landscape photo",
    ],
    '城市建築': [
        "街景與建築物", "a photo of city streets and buildings", "建築物", "城市街景", "房屋、大樓、教堂或寺廟等建築物", "建築物的外觀", "市區的街道",
        "a photo of a building", "architecture", "a building such as a house, tower, church or temple",
        "an urban cityscape",
    ],
    '美食': [
        "食物與餐點", "a photo of food or a meal", "一道菜", "餐廳的餐點", "食物的照片", "飲料或甜點", "桌上的食物", "a dish of food",
        "food and drinks", "a dessert or a drink",
    ],
    '花草植物': [
        "花與植物", "a photo of flowers or plants", "花朵", "植物盆栽", "樹木或草地", "盛開的花", "a close-up of a flower",
        "green plants", "a potted plant",
    ],
    '交通工具': [
        "汽車、機車、火車、飛機", "a photo of a car, motorcycle, train or airplane", "車輛", "一輛車", "交通工具的照片", "卡車或公車",
        "船或飛機", "a vehicle", "a photo of a vehicle such as a car, truck, bus, train, boat or airplane",
        "a photo of a car",
    ],
    '螢幕截圖': [
        "手機或電腦的截圖", "a screenshot of a phone or computer screen", "手機畫面截圖", "聊天對話截圖", "網頁或應用程式畫面的截圖", "電腦螢幕畫面",
        "a screenshot of a mobile app", "a screenshot of a chat conversation", "a screenshot of a website",
        "a screenshot",
    ],
    '文件收據': [
        "文件、收據、紙本文字", "a photo of a document, receipt or paper with text", "拍下來的文件", "收據或發票", "印有文字的紙張",
        "表單或合約", "手寫的筆記", "a scanned document", "a receipt or invoice", "a page of printed text",
    ],
    '動漫插畫': [
        "卡通或動漫圖", "an anime or cartoon illustration", "動漫角色", "插畫", "手繪圖", "卡通貼圖", "漫畫", "a cartoon drawing",
        "an anime character", "a digital art illustration", "a cartoon sticker",
    ],
    '迷因梗圖': [
        "迷因梗圖", "有文字的搞笑圖片", "梗圖", "加了字幕的搞笑圖", "a meme with text", "an internet meme",
        "a funny image macro with a caption",
    ],
    '海報廣告': [
        "海報", "廣告", "宣傳單", "促銷廣告圖", "活動宣傳海報", "a poster", "an advertisement", "a promotional flyer",
        "a sale banner",
    ],
    '商品': [
        "商品照片", "網拍商品", "產品圖", "a product photo", "a product on a white background",
    ],
    '圖表': [
        "圖表", "統計圖", "表格", "簡報投影片", "a chart or graph", "a table of numbers", "a presentation slide",
    ],
}
# 同一組描述的其他名稱
ALIASES = {"廣告": "海報廣告", "海報": "海報廣告", "梗圖": "迷因梗圖", "迷因": "迷因梗圖", "截圖": "螢幕截圖", "食物": "美食",
           "建築": "城市建築", "插畫": "動漫插畫", "動漫": "動漫插畫", "文件": "文件收據", "收據": "文件收據"}

# 舊版的預設描述。使用者的分類如果還是這一組（沒改過），讀取時自動換成新的；改過的不動。
LEGACY_DEFAULT_PROMPTS: dict[str, list[str]] = {
    '人像': [
        "人物照片", "自拍", "a photo of a person", "a selfie",
    ],
    '合照': [
        "一群人的合照", "a group photo of several people",
    ],
    '貓': [
        "貓咪", "a photo of a cat",
    ],
    '狗': [
        "狗狗", "a photo of a dog",
    ],
    '風景': [
        "自然風景", "山、海、天空", "a landscape photo of mountains, sea, sky or nature",
    ],
    '城市建築': [
        "街景與建築物", "a photo of city streets and buildings",
    ],
    '美食': [
        "食物與餐點", "a photo of food or a meal",
    ],
    '花草植物': [
        "花與植物", "a photo of flowers or plants",
    ],
    '交通工具': [
        "汽車、機車、火車、飛機", "a photo of a car, motorcycle, train or airplane",
    ],
    '螢幕截圖': [
        "手機或電腦的截圖", "a screenshot of a phone or computer screen",
    ],
    '文件收據': [
        "文件、收據、紙本文字", "a photo of a document, receipt or paper with text",
    ],
    '動漫插畫': [
        "卡通或動漫圖", "an anime or cartoon illustration",
    ],
}


def builtin_prompts(name: str) -> list[str]:
    """這個名稱的內建描述；常見但沒有整組描述的名稱給一句英文，其他回傳空的。"""
    name = name.strip()
    prompts = BUILTIN_PROMPTS.get(ALIASES.get(name, name))
    if prompts:
        return list(prompts)
    hint = category_hint(name)
    return [f"a photo of {hint}"] if hint else []


def upgrade_prompts(name: str, prompts: list[str]) -> list[str]:
    """讀取使用者的分類時：舊版預設描述（沒被改過）→ 新版；有整組內建描述、卻沒寫任何說明的名稱（例如自己加的
    「廣告」）→ 帶入內建描述（實測：海報分得進去的比例差不多，但別類的照片被誤拉進來的少了三分之二）。
    使用者自己寫過的說明一律原樣保留。"""
    key = ALIASES.get(name, name)
    if name in LEGACY_DEFAULT_PROMPTS and prompts == LEGACY_DEFAULT_PROMPTS[name]:
        return list(BUILTIN_PROMPTS[name])
    if not prompts and key in BUILTIN_PROMPTS:
        return list(BUILTIN_PROMPTS[key])
    return prompts
