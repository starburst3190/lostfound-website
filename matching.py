"""語意媒合：以 Jina embeddings v3 產生向量，計算遺失通報與招領物的相似度。

設計重點
--------
* **語意 + 結構化混合評分**：embedding 向量的 cosine 相似度負責「意思相近」
  （例如「錢」≈「現金」、「皮夾」≈「錢包」），再加上類型 / 地點 / 時間等結構化訊號。
* **向量儲存**：embedding 以 JSON 陣列存在 Postgres 的 text 欄位
  （``lost_items.embedding`` / ``lost_reports.embedding``），在 Python 端算 cosine；
  資料量還小，暴力法完全夠用。資料量變大要改用 pgvector 索引時見 ``supabase/pgvector.sql``。
  （embedding 的讀寫與媒合流程在 ``app.py``，本模組只提供純函式。）
* **優雅降級**：若未設定 ``JINA_API_KEY`` 或呼叫失敗，會退回原本的關鍵字重疊比對，
  服務仍可運作（方便本機開發與測試）。
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# --- Jina embeddings v3 設定 ---
# 金鑰於呼叫時才讀取（lazy），避免相依於 load_dotenv() 與 import 的先後順序。
JINA_URL = "https://api.jina.ai/v1/embeddings"
JINA_MODEL = "jina-embeddings-v3"
# text-matching：對稱式語意相似，適合「通報 vs 招領物」的比對情境。
JINA_TASK = "text-matching"
EMBED_DIM = 1024
JINA_TIMEOUT = 60  # 批次請求較大，給寬一點的逾時秒數

# --- 評分權重 ---
# 設計原則：物品「本身」（名稱 / 描述）的語意相似度才是主訊號；類型 / 地點 / 時間只是輔助，
# 用來在「名稱已相近」的候選之間做排序與加強信心。
#
# 關鍵不變式：STRUCTURED_MAX（類型 + 地點 + 時間的上限）< MATCH_THRESHOLD。
# 因此「光靠類型 + 地點 + 時間」永遠跨不過門檻 —— 一定要名稱 / 語意有一定相似度，
# 才補得上差距而成立媒合。這修正了舊版「類型 25 + 地點 15 = 40，再湊任一條件就跨過 45」
# 的誤判（例：同類型、同地點、同一天，但一個是筆電、一個是耳機，也會被判為媒合）。
CATEGORY_POINTS = 15      # 類型一致（正規類別只有約 11 種，是弱證據，故權重不高）
LOCATION_POINTS = 10      # 地點相近
TIME_IN_RANGE_POINTS = 12 # 拾獲日落在通報的遺失日期區間內的分數
TIME_OK_POINTS = 6        # 距區間 <= 2 天的分數
TIME_NEAR_POINTS = 3      # 距區間 <= 5 天的分數
TIME_SLACK_OK_DAYS = 2
TIME_SLACK_NEAR_DAYS = 5
# 類型 + 地點 + 時間全中也只有 37 分，刻意 < MATCH_THRESHOLD（見上方不變式）。
STRUCTURED_MAX = CATEGORY_POINTS + LOCATION_POINTS + TIME_IN_RANGE_POINTS
SEMANTIC_MAX = 60         # 語意（名稱 / 描述）相似最高加分 —— 全場最大的單一權重
SEMANTIC_FLOOR = 0.40     # cosine 低於此值不計語意分（過濾雜訊；提高門檻讓「名稱相近」更嚴格）
KEYWORD_MAX = 30          # 無向量時的關鍵字 fallback 上限
KEYWORD_PER_TOKEN = 7     # 每個重疊關鍵字的分數（無向量 fallback 用）
MATCH_THRESHOLD = 45      # 達到此分數才視為一筆媒合（> STRUCTURED_MAX，故名稱訊號為必要條件）
SCORE_CAP = 99

# 防呆：若日後有人調權重把這條不變式弄壞（結構化訊號又能單獨成立），import 時就會炸出來。
assert STRUCTURED_MAX < MATCH_THRESHOLD, (
    "結構化訊號（類型/地點/時間）的總分不應 >= 媒合門檻，否則名稱不相近也會誤判為媒合"
)

# --- 台大校內地點簡稱 → 正式名稱 ---
# 校內慣用簡稱（如「活大」）一般 embedding 模型不認得，且地點是用結構化比對而非語意，
# 因此用這張對照表把簡稱正規化後再比。
#
# 對照表存在 location_aliases.json，方便非工程背景的貢獻者直接增修（不必動程式）。
# 這份 JSON 的格式刻意做成「簡稱: 正式名稱」的扁平結構，之後搬到 Supabase 時可直接
# 對應一張 location_aliases 資料表，改由後台維護、免重新部署。
# 下方的 _FALLBACK_ALIASES 是 JSON 缺失或格式錯誤時的內建預設，確保服務仍可運作。
_FALLBACK_ALIASES: dict[str, str] = {
    "活大": "第一學生活動中心",
    "總圖": "總圖書館",
    "小福": "小福樓",
}
_ALIASES_PATH = Path(__file__).resolve().parent / "location_aliases.json"


def _load_location_aliases() -> dict[str, str]:
    try:
        with _ALIASES_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            return data
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        pass
    return dict(_FALLBACK_ALIASES)


LOCATION_ALIASES: dict[str, str] = _load_location_aliases()
# 先換較長的簡稱，避免「第一活動中心」被「活」之類的短鍵搶先替換。
_ALIAS_ORDER = sorted(LOCATION_ALIASES, key=len, reverse=True)
_CANONICAL_PLACES = set(LOCATION_ALIASES.values())


# --- 物品類型正規化 ---
# 各來源（圖書館等）的類型字串很雜（如「其他 充電線」「影印卡、悠遊卡」），和使用者通報
# 表單的固定選項對不上，導致「類型一致」加分與類型篩選幾乎失效。這裡用關鍵字規則把任意
# 類型字串收斂成一組正規類別；通報表單與篩選也都用同一組類別，確保兩邊可對齊。
#
# 規則存在 category_rules.json（正規類別 → 關鍵字清單），方便直接增修。
# 比對採「由上到下、命中關鍵字即歸類」，所以順序有意義（例：現金/錢包 要在 包包 之前，
# 「錢包」才不會被「包」搶走）。最後一類請保留為 fallback（關鍵字清單留空）。
_FALLBACK_CATEGORY_RULES: dict[str, list[str]] = {
    "電子產品": ["充電", "傳輸線", "耳機", "滑鼠", "鍵盤", "隨身碟", "行動電源", "轉接", "usb"],
    "證件/卡片": ["學生證", "證件", "悠遊卡", "影印卡", "卡片"],
    "現金/錢包": ["現金", "錢包", "皮夾"],
    "鑰匙": ["鑰匙", "鑰"],
    "雨傘": ["雨傘", "傘"],
    "水壺/餐具": ["水壺", "水杯", "保溫"],
    "書籍/文具": ["筆記本", "期刊", "圖書"],
    "包包": ["背包", "後背", "包包"],
    "衣物/配件": ["外套", "衣物", "帽"],
    "其他": [],
}
_CATEGORY_RULES_PATH = Path(__file__).resolve().parent / "category_rules.json"


def _load_category_rules() -> dict[str, list[str]]:
    try:
        with _CATEGORY_RULES_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and all(
            isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
            for k, v in data.items()
        ) and data:
            return data
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        pass
    return dict(_FALLBACK_CATEGORY_RULES)


CATEGORY_RULES: dict[str, list[str]] = _load_category_rules()
# 通報表單 / 篩選用的正規類別清單（順序即 JSON 的順序）。
CANONICAL_CATEGORIES: list[str] = list(CATEGORY_RULES.keys())
_CATEGORY_FALLBACK = CANONICAL_CATEGORIES[-1] if CANONICAL_CATEGORIES else "其他"


def canonical_category(raw: str | None) -> str:
    """把任意來源的類型字串收斂成一組正規類別；對不上時歸到最後一類（其他）。"""
    if not raw:
        return _CATEGORY_FALLBACK
    text = raw.lower()
    for canonical, keywords in CATEGORY_RULES.items():
        for kw in keywords:
            if kw.lower() in text:
                return canonical
    return _CATEGORY_FALLBACK


# ---------------------------------------------------------------------------
# Embedding 客戶端
# ---------------------------------------------------------------------------
def _api_key() -> str | None:
    return os.environ.get("JINA_API_KEY")


def embeddings_enabled() -> bool:
    return bool(_api_key())


def embed_texts(texts: list[str]) -> list[list[float]]:
    """呼叫 Jina API，回傳與輸入順序一致的向量清單。"""
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("JINA_API_KEY 未設定")
    payload = json.dumps(
        {
            "model": JINA_MODEL,
            "task": JINA_TASK,
            "dimensions": EMBED_DIM,
            "embedding_type": "float",
            "input": texts,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        JINA_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # 帶上 User-Agent，避免被 Cloudflare 擋下預設的 Python-urllib 簽章（error 1010）。
            "User-Agent": "ntu-lostfound/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=JINA_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    rows = sorted(body["data"], key=lambda d: d["index"])
    return [row["embedding"] for row in rows]


def embed_text(text: str) -> list[float]:
    return embed_texts([text])[0]


def item_text(row) -> str:
    """招領物 / 通報共用的語意文字：以物品本身的標題與描述為主。"""
    return f"{row['title']}。{row['description']}"


# ---------------------------------------------------------------------------
# 向量運算
# ---------------------------------------------------------------------------
def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _semantic_points(cos: float) -> int:
    if cos <= SEMANTIC_FLOOR:
        return 0
    scaled = (cos - SEMANTIC_FLOOR) / (1.0 - SEMANTIC_FLOOR)
    return round(SEMANTIC_MAX * scaled)


# ---------------------------------------------------------------------------
# 結構化 / 關鍵字評分
# ---------------------------------------------------------------------------
def normalize_words(text: str) -> set[str]:
    clean = text.lower()
    for token in ["：", "，", ",", ".", "(", ")", "[", "]", "{", "}", "<", ">", "。"]:
        clean = clean.replace(token, " ")
    return {piece for piece in clean.split() if piece}


def canonical_location(text: str) -> str:
    """把校內慣用簡稱（活大、總圖…）展開成正式名稱，方便地點比對。"""
    result = text
    for alias in _ALIAS_ORDER:
        canon = LOCATION_ALIASES[alias]
        # 已是正式名稱就略過，避免重複展開（如「普通教學館」→「普通教學館教學館」）。
        if canon in result:
            continue
        if alias in result:
            result = result.replace(alias, canon)
    return result.lower()


def _location_match(report_location: str, external_location: str) -> bool:
    r = canonical_location(report_location)
    e = canonical_location(external_location)
    # 兩邊（正規化後）提到同一個已知地點 → 視為相近。
    for place in _CANONICAL_PLACES:
        p = place.lower()
        if p in r and p in e:
            return True
    # 退而求其次：正規化後的前綴重疊（沿用原本的粗略啟發式）。
    return bool(r[:2]) and (r[:2] in e or e[:2] in r)


def _as_date(value):
    """把日期或日期時間字串轉成 date；失敗則回傳 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _time_score(report, external) -> tuple[int, str | None]:
    """以「天」為精度，比對通報的遺失日期區間與招領物的拾獲日期。

    使用者常記不清確切時間、圖書館資料也只有日期，所以用區間 + 寬限天數，
    而非精確到分鐘的單一時刻。
    """
    start = _as_date(report.get("lost_date_start"))
    end = _as_date(report.get("lost_date_end")) or start
    found = _as_date(external.get("found_at"))
    if start is None or found is None:
        return 0, None
    if end < start:
        start, end = end, start
    if start <= found <= end:
        return TIME_IN_RANGE_POINTS, "時間吻合"
    gap = (start - found).days if found < start else (found - end).days
    if gap <= TIME_SLACK_OK_DAYS:
        return TIME_OK_POINTS, "時間接近"
    if gap <= TIME_SLACK_NEAR_DAYS:
        return TIME_NEAR_POINTS, "時間大致符合"
    return 0, None


def _structured_score(report, external) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if report["category"] == external["category"]:
        score += CATEGORY_POINTS
        reasons.append("類型一致")
    if _location_match(report["location"], external["location"]):
        score += LOCATION_POINTS
        reasons.append("地點相近")
    time_points, time_reason = _time_score(report, external)
    if time_points:
        score += time_points
        reasons.append(time_reason)
    return score, reasons


def _keyword_score(report, external) -> tuple[int, list[str]]:
    shared = normalize_words(report["title"] + " " + report["description"]).intersection(
        normalize_words(external["title"] + " " + external["description"])
    )
    if not shared:
        return 0, []
    points = min(KEYWORD_MAX, len(shared) * KEYWORD_PER_TOKEN)
    return points, ["關鍵字重疊：" + "、".join(sorted(shared)[:4])]


def blended_score(report, external, cos: float | None) -> tuple[int, list[str]]:
    """結合結構化訊號與語意（或關鍵字 fallback）的最終分數。"""
    score, reasons = _structured_score(report, external)
    if cos is not None:
        points = _semantic_points(cos)
        if points:
            score += points
            reasons.append(f"語意相近（{round(cos * 100)}%）")
    else:
        points, kw_reasons = _keyword_score(report, external)
        score += points
        reasons.extend(kw_reasons)
    return min(score, SCORE_CAP), reasons


