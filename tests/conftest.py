from __future__ import annotations

from copy import deepcopy

import pytest

import app as webapp
from frontend_items import FRONTEND_ITEMS


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
        "external_items": deepcopy(FRONTEND_ITEMS),
        "reports": [],
        "matches": [],
        "notifications": [],
        "source_locks": {"facebook": True},
    }
