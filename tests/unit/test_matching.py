from __future__ import annotations

from datetime import date

import pytest

import matching


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("白色 AirPods Pro 耳機", "電子產品"),
        ("台大學生證", "證件/卡片"),
        ("黑色折疊傘", "雨傘"),
        ("Nike 後背包", "包包"),
        ("無法分類的物品", "其他"),
    ],
)
def test_canonical_category(raw, expected):
    assert matching.canonical_category(raw) == expected


@pytest.mark.unit
def test_canonical_location_expands_ntu_aliases():
    assert matching.canonical_location("二活三樓") == "第二學生活動中心三樓"
    assert matching.canonical_location("活大一樓") == "第一學生活動中心一樓"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("found_at", "expected_points", "expected_reason"),
    [
        ("2026-06-07T12:00:00", matching.TIME_IN_RANGE_POINTS, "時間吻合"),
        ("2026-06-09T12:00:00", matching.TIME_OK_POINTS, "時間接近"),
        ("2026-06-12T12:00:00", matching.TIME_NEAR_POINTS, "時間大致符合"),
        ("2026-06-13T12:00:00", 0, None),
    ],
)
def test_time_score_uses_report_date_range(found_at, expected_points, expected_reason):
    report = {
        "lost_date_start": date(2026, 6, 6).isoformat(),
        "lost_date_end": date(2026, 6, 7).isoformat(),
    }

    points, reason = matching._time_score(report, {"found_at": found_at})

    assert points == expected_points
    assert reason == expected_reason


@pytest.mark.unit
def test_blended_score_crosses_threshold_for_close_report():
    report = {
        "title": "AirPods Pro 不見了",
        "category": "電子產品",
        "location": "二活三樓",
        "lost_date_start": "2026-06-07",
        "lost_date_end": "2026-06-07",
        "description": "白色耳機盒，灰色保護套",
    }
    item = {
        "title": "白色 AirPods Pro 耳機",
        "category": "電子產品",
        "location": "第二學生活動中心三樓",
        "found_at": "2026-06-07T12:35:00",
        "description": "白色 AirPods Pro 充電盒，外殼有灰色保護套。",
    }

    score, reasons = matching.blended_score(report, item, cos=None)

    assert score >= matching.MATCH_THRESHOLD
    assert "類型一致" in reasons
    assert "地點相近" in reasons
    assert "時間吻合" in reasons


@pytest.mark.unit
def test_blended_score_rejects_unrelated_report():
    report = {
        "title": "紅色圍巾",
        "category": "衣物/配件",
        "location": "管理學院",
        "lost_date_start": "2026-05-01",
        "lost_date_end": "2026-05-01",
        "description": "羊毛材質",
    }
    item = {
        "title": "白色 AirPods Pro 耳機",
        "category": "電子產品",
        "location": "第二學生活動中心三樓",
        "found_at": "2026-06-07T12:35:00",
        "description": "灰色保護套",
    }

    score, reasons = matching.blended_score(report, item, cos=None)

    assert score < matching.MATCH_THRESHOLD
    assert reasons == []


@pytest.mark.unit
def test_structured_signals_alone_do_not_cross_threshold():
    """類型 + 地點 + 時間全中，但名稱毫不相關 → 不應成立媒合（名稱為必要條件）。

    這是「同類型、同地點、同一天，但一個是雨傘、一個是耳機」的情境，舊權重會誤判為媒合。
    """
    report = {
        "title": "黑色自動傘",
        "category": "電子產品",
        "location": "總圖書館",
        "lost_date_start": "2026-06-07",
        "lost_date_end": "2026-06-07",
        "description": "一鍵開合",
    }
    item = {
        "title": "白色無線耳機",
        "category": "電子產品",
        "location": "總圖書館",
        "found_at": "2026-06-07T10:00:00",
        "description": "藍牙連線",
    }

    score, reasons = matching.blended_score(report, item, cos=None)

    # 結構化分數有累積（三項都中），但刻意低於門檻，光靠它們不足以成立。
    assert matching.STRUCTURED_MAX < matching.MATCH_THRESHOLD
    assert score == matching.STRUCTURED_MAX
    assert score < matching.MATCH_THRESHOLD
    assert "類型一致" in reasons and "地點相近" in reasons and "時間吻合" in reasons


@pytest.mark.unit
def test_semantic_name_similarity_drives_match():
    """名稱語意高度相似即可成立媒合，即使類型 / 地點 / 時間都對不上；反之語意太低則不成立。"""
    report = {
        "title": "錢包",
        "category": "現金/錢包",
        "location": "甲處",
        "lost_date_start": "2026-06-01",
        "lost_date_end": "2026-06-01",
        "description": "棕色皮夾",
    }
    item = {
        "title": "皮夾",
        "category": "其他",            # 類別不同
        "location": "乙處",            # 地點不同
        "found_at": "2026-06-20T10:00:00",  # 時間差很多
        "description": "棕色錢包",
    }

    high_score, high_reasons = matching.blended_score(report, item, cos=0.9)
    low_score, _ = matching.blended_score(report, item, cos=0.30)

    assert high_score >= matching.MATCH_THRESHOLD
    assert any("語意相近" in r for r in high_reasons)
    # cos 低於 SEMANTIC_FLOOR → 不計語意分，且結構化全不中 → 不成立。
    assert low_score < matching.MATCH_THRESHOLD
