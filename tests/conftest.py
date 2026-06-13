from __future__ import annotations

from copy import deepcopy

import pytest

import app as webapp


TEST_ITEMS = [
    {
        "source_ref": "school_libraries:test-umbrella",
        "title": "黑色折疊傘",
        "category": "雨傘",
        "location": "總圖書館二樓",
        "found_at": "2026-06-07T11:20:00",
        "description": "黑色折疊傘，存放於一樓服務台。",
        "source_name": "總圖書館",
        "source_type": "library",
        "source_url": "",
    },
]


@pytest.fixture
def app():
    webapp.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return webapp.app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def frontend_bundle():
    return {
        "external_items": deepcopy(TEST_ITEMS),
        "reports": [],
        "matches": [],
        "notifications": [],
        "source_locks": {"facebook": False},
    }
