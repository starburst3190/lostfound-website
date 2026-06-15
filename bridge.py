"""欄位對映層：把爬蟲寫進 Supabase ``lost_items`` 的資料，映射成前端 / 媒合用的
``external`` 形狀（title / found_at / source_name 等）。

背景
----
爬蟲（``scripts/scrapers/supa_crawl_lib.py``）把校園失物 upsert 進 Supabase 的
``lost_items``；前端與媒合引擎直接讀這張表，再透過這裡的 ``lost_item_to_external()``
把欄位補齊成 UI / 媒合需要的形狀。``lost_items`` 沒有的欄位（title、found_at、
source_name / type / url）就在這裡合成。

欄位對照
--------
``lost_items``                     ->  ``external_items``
  source_system + original_id      ->  source_ref（去重用）
  description                      ->  title（截斷合成）、description
  category                         ->  category
  location                         ->  location
  found_date（2026/06/05）          ->  found_at（ISO）
  storage_place                    ->  併入 description
  (無)                             ->  source_name / source_type（由 source_system 推得）
  (無)                             ->  source_url（圖書館來源為空）
"""

from __future__ import annotations

from datetime import datetime

import matching

# source_system -> (顯示用來源名稱, 來源類型)
# 來源類型沿用 external_items.source_type；facebook 類型在通報通知時會附原始連結。
SOURCE_SYSTEM_MAP: dict[str, tuple[str, str]] = {
    "school_libraries": ("總圖書館", "library"),
    "campus_police": ("駐警隊", "police"),
    # campus_police_raw（爬蟲保留的原始整則公告）不在此列：app.py 的查詢以
    # _EXCLUDE_RAW_SQL 過濾掉 *_raw，不應出現在前端 / 媒合。
    # 之後新增來源時在此擴充，例如：
    # "fb_exchange": ("FB交流版", "facebook"),
}

_TITLE_MAX = 40

# 爬蟲日期常見格式（只有日期，沒有時間）。
_DATE_FORMATS = (
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
)


def parse_found_date(raw: str | None) -> str:
    """把爬蟲的拾獲日期轉成 ISO 字串；無法解析時退回當下時間（避免媒合的時間計算炸掉）。"""
    if raw:
        text = raw.strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).isoformat(timespec="seconds")
            except ValueError:
                continue
    return datetime.now().isoformat(timespec="seconds")


def _title_from(description: str | None, category: str | None) -> str:
    desc = (description or "").strip()
    if desc:
        return desc[:_TITLE_MAX]
    return (category or "拾獲物品").strip()


def lost_item_to_external(row: dict) -> dict:
    """把單筆 ``lost_items`` 列映射成可寫入 ``external_items`` 的 dict。"""
    source_system = (row.get("source_system") or "unknown").strip()
    source_name, source_type = SOURCE_SYSTEM_MAP.get(source_system, (source_system, "other"))

    description = (row.get("description") or "").strip()
    storage_place = (row.get("storage_place") or "").strip()
    if storage_place:
        description = f"{description}（存放：{storage_place}）".strip()

    return {
        "source_ref": f"{source_system}:{row.get('original_id')}",
        "title": _title_from(row.get("description"), row.get("category")),
        # 把來源端雜亂的類型字串收斂成一組正規類別，讓篩選與「類型一致」加分能對齊。
        # 來源的 category 常常只有「其他」，真正的品名在 description，所以一起拿去分類。
        "category": matching.canonical_category(f"{row.get('category') or ''} {row.get('description') or ''}"),
        "location": (row.get("location") or "").strip(),
        "found_at": parse_found_date(row.get("found_date")),
        "description": description,
        "source_name": source_name,
        "source_type": source_type,
        "source_url": "",
    }
