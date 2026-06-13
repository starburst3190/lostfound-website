from __future__ import annotations

import pytest

import bridge


@pytest.mark.unit
def test_lost_item_to_external_maps_scraper_row():
    external = bridge.lost_item_to_external(
        {
            "source_system": "school_libraries",
            "original_id": "A123",
            "found_date": "2026/06/07",
            "location": "總圖2F",
            "description": "黑色折疊傘",
            "category": "其他",
            "storage_place": "一樓服務台",
        }
    )

    assert external["source_ref"] == "school_libraries:A123"
    assert external["source_name"] == "總圖書館"
    assert external["source_type"] == "library"
    assert external["category"] == "雨傘"
    assert external["found_at"] == "2026-06-07T00:00:00"
    assert "存放：一樓服務台" in external["description"]
