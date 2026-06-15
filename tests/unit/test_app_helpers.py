from __future__ import annotations

import pytest

import app as webapp


@pytest.mark.unit
@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("student@ntu.edu.tw", True),
        ("STUDENT@NTU.EDU.TW", True),
        ("student@ntu.edu.tw.example.com", False),
        ("student@gmail.com", False),
    ],
)
def test_is_ntu_email(email, expected):
    assert webapp.is_ntu_email(email) is expected


@pytest.mark.unit
def test_read_report_form_normalizes_reversed_dates(app):
    with app.test_request_context(
        "/report",
        method="POST",
        data={
            "title": "黑色手機",
            "category": "電子產品",
            "location": "總圖",
            "lost_date_start": "2026-06-07",
            "lost_date_end": "2026-06-05",
            "description": "透明保護殼",
        },
    ):
        data = webapp._read_report_form()

    assert data["lost_date_start"] == "2026-06-05"
    assert data["lost_date_end"] == "2026-06-07"


@pytest.mark.unit
def test_build_match_email_bundles_multiple_results():
    report = {"id": 1, "title": "遺失耳機"}
    pairs = [
        (
            report,
            {
                "title": "白色 AirPods Pro",
                "source_name": "FB交流版",
                "source_type": "facebook",
                "source_url": "https://www.facebook.com/",
                "location": "二活三樓",
            },
        ),
        (
            report,
            {
                "title": "白色耳機盒",
                "source_name": "駐警隊",
                "source_type": "police",
                "source_url": "",
                "location": "新生南路校門",
            },
        ),
    ]

    subject, body = webapp._build_match_email(pairs)

    assert subject == "新的遺失物媒合結果（共 2 筆）"
    assert body.count("你的遺失通報「遺失耳機」") == 1
    assert "白色 AirPods Pro" in body
    assert "白色耳機盒" in body
    assert webapp.MATCH_DISCLAIMER in body


@pytest.mark.unit
def test_build_match_notification_bundles_batch_into_single_message():
    """同一批的多筆配對應彙整成「一則」站內通知（標頭只出現一次）。"""
    report = {"id": 1, "title": "遺失耳機"}
    pairs = [
        (report, {"title": "白色 AirPods Pro", "source_name": "FB交流版", "location": "二活三樓"}),
        (report, {"title": "白色耳機盒", "source_name": "駐警隊", "location": "新生南路校門"}),
    ]

    subject, message = webapp._build_match_notification(pairs)

    assert subject == "新的遺失物媒合結果（共 2 筆）"
    # 同一份通報只出現一次標頭，兩個招領物列在同一則訊息裡。
    assert message.count("你的遺失通報「遺失耳機」") == 1
    assert "白色 AirPods Pro" in message
    assert "白色耳機盒" in message
