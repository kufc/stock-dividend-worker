"""內建常見詞彙：用來回答「這看起來像什麼」，並讓使用者一鍵把看到的東西新增為分類。

每一項是 (中文名稱, 英文描述)；英文描述能讓多語言 CLIP 判斷得更準。
"""

VOCABULARY: list[tuple[str, str]] = [
    # 人物
    ("人物", "a person"), ("自拍", "a selfie"), ("合照", "a group of people"), ("嬰兒", "a baby"),
    ("小孩", "a child"), ("老人", "an elderly person"), ("新娘", "a bride at a wedding"),
    ("運動員", "an athlete playing sports"), ("演唱會", "a concert with a crowd"), ("派對", "a party"),
    # 動物
    ("貓", "a cat"), ("狗", "a dog"), ("鳥", "a bird"), ("魚", "a fish"), ("兔子", "a rabbit"),
    ("倉鼠", "a hamster"), ("馬", "a horse"), ("牛", "a cow"), ("昆蟲", "an insect"), ("蝴蝶", "a butterfly"),
    ("爬蟲類", "a reptile such as a lizard or snake"), ("野生動物", "wild animals"), ("水族箱", "an aquarium"),
    # 自然
    ("山", "mountains"), ("海灘", "a beach"), ("海洋", "the ocean"), ("湖泊", "a lake"), ("河流", "a river"),
    ("森林", "a forest"), ("沙漠", "a desert"), ("雪景", "snow"), ("夕陽", "a sunset"), ("日出", "a sunrise"),
    ("星空", "a starry night sky"), ("雲", "clouds in the sky"), ("瀑布", "a waterfall"), ("花", "flowers"),
    ("樹", "trees"), ("草地", "grass field"), ("彩虹", "a rainbow"), ("雨天", "rain"),
    # 城市與地點
    ("城市夜景", "a city at night"), ("街道", "a street"), ("高樓", "skyscrapers"), ("橋", "a bridge"),
    ("寺廟", "a temple"), ("教堂", "a church"), ("古蹟", "a historic monument"), ("公園", "a park"),
    ("遊樂園", "an amusement park"), ("博物館", "a museum"), ("機場", "an airport"), ("車站", "a train station"),
    ("商店", "a shop or store"), ("夜市", "a night market"), ("餐廳", "a restaurant"), ("咖啡廳", "a cafe"),
    ("辦公室", "an office"), ("教室", "a classroom"), ("臥室", "a bedroom"), ("客廳", "a living room"),
    ("廚房", "a kitchen"), ("浴室", "a bathroom"), ("室內", "an indoor room"), ("戶外", "outdoors"),
    ("健身房", "a gym"), ("游泳池", "a swimming pool"), ("露營", "camping with a tent"),
    # 食物
    ("美食", "food"), ("甜點", "a dessert"), ("蛋糕", "a cake"), ("飲料", "a drink"), ("咖啡", "coffee"),
    ("手搖飲", "bubble tea"), ("水果", "fruit"), ("麵食", "noodles"), ("火鍋", "hot pot"), ("燒烤", "barbecue"),
    ("壽司", "sushi"), ("披薩", "pizza"), ("漢堡", "a hamburger"), ("早餐", "breakfast"), ("便當", "a bento lunch box"),
    # 交通
    ("汽車", "a car"), ("機車", "a motorcycle or scooter"), ("腳踏車", "a bicycle"), ("公車", "a bus"),
    ("火車", "a train"), ("飛機", "an airplane"), ("船", "a boat"), ("高速公路", "a highway"),
    # 物品
    ("手機", "a smartphone"), ("電腦", "a computer"), ("電視", "a television"), ("相機", "a camera"),
    ("書", "books"), ("衣服", "clothes"), ("鞋子", "shoes"), ("包包", "a bag"), ("手錶", "a watch"),
    ("珠寶", "jewelry"), ("化妝品", "cosmetics and makeup"), ("玩具", "toys"), ("公仔", "a figurine"),
    ("家具", "furniture"), ("沙發", "a sofa"), ("床", "a bed"), ("植栽", "potted plants"), ("樂器", "a musical instrument"),
    ("球類運動", "a ball game"), ("電玩遊戲", "a video game"), ("聖誕節", "christmas decorations"),
    ("生日", "a birthday celebration"), ("煙火", "fireworks"), ("禮物", "a gift box"),
    # 文件與數位
    ("螢幕截圖", "a screenshot of a phone or computer screen"), ("聊天截圖", "a screenshot of a chat conversation"),
    ("文件", "a document with text"), ("收據", "a receipt"), ("發票", "an invoice"), ("名片", "a business card"),
    ("證件", "an ID card"), ("地圖", "a map"), ("圖表", "a chart or graph"), ("表格", "a spreadsheet table"),
    ("白板", "a whiteboard"), ("手寫筆記", "handwritten notes"), ("QR Code", "a QR code"), ("迷因梗圖", "a meme with text"),
    ("海報", "a poster"), ("標誌", "a logo"), ("動漫", "an anime illustration"), ("插畫", "a drawing or illustration"),
    ("漫畫", "a comic"), ("3D 繪圖", "a 3D render"), ("藝術作品", "a painting artwork"),
    # 影像特性
    ("黑白照片", "a black and white photo"), ("模糊照片", "a blurry photo"), ("夜間照片", "a dark photo taken at night"),
    ("特寫", "a close-up photo"), ("空拍", "an aerial drone photo"), ("全景", "a panorama photo"),
]
