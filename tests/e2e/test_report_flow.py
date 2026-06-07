from __future__ import annotations

import socket
import threading
from copy import deepcopy

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server

import app as webapp
from frontend_items import FRONTEND_ITEMS


class InsertResult:
    def fetchone(self):
        return {"id": 101}


class InsertConnection:
    def execute(self, query, params=()):
        assert "INSERT INTO lost_reports" in query
        return InsertResult()

    def commit(self):
        pass

    def close(self):
        pass


def _available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(monkeypatch):
    state = {"reports": [], "matches": []}

    def get_user(user_id):
        return {"id": user_id, "email": "student@ntu.edu.tw"}

    def fetch_bundle(user_id):
        external_items = deepcopy(FRONTEND_ITEMS)
        source_locks = {"facebook": False}
        if not user_id:
            external_items = [item for item in external_items if item["source_type"] != "facebook"]
            source_locks = {"facebook": True}
        return {
            "external_items": external_items,
            "reports": deepcopy(state["reports"]),
            "matches": deepcopy(state["matches"]),
            "notifications": [],
            "source_locks": source_locks,
        }

    def run_matching(report_id):
        state["reports"].append(
            {
                "id": report_id,
                "title": "AirPods Pro 不見了",
                "category": "電子產品",
                "location": "二活三樓",
                "lost_date_start": "2026-06-07",
                "lost_date_end": "2026-06-07",
                "description": "白色耳機盒，灰色保護套",
                "status": "open",
            }
        )
        state["matches"].append(
            {
                "id": 1,
                "score": 83,
                "reasons_json": '["類型一致", "地點相近", "時間吻合", "語意相近（75%）"]',
                "created_at": "2026-06-07T15:00:00",
                "report_title": "AirPods Pro 不見了",
                "external_title": "白色 AirPods Pro 耳機",
                "external_location": "第二學生活動中心三樓",
                "external_source_name": "FB交流版",
                "external_source_type": "facebook",
                "external_source_url": "https://www.facebook.com/",
            }
        )

    monkeypatch.setattr(webapp, "get_user", get_user)
    monkeypatch.setattr(webapp, "fetch_bundle", fetch_bundle)
    monkeypatch.setattr(webapp, "get_db", InsertConnection)
    monkeypatch.setattr(webapp, "run_matching", run_matching)
    webapp.app.config.update(TESTING=False, SECRET_KEY="e2e-secret")

    port = _available_port()
    server = make_server("127.0.0.1", port, webapp.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def browser():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1440,1200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    yield driver
    driver.quit()


def _login_browser(browser, live_server):
    client = webapp.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = 7
    session_cookie = client.get_cookie(webapp.app.config["SESSION_COOKIE_NAME"])

    browser.get(live_server)
    browser.add_cookie(
        {
            "name": session_cookie.key,
            "value": session_cookie.value,
            "path": session_cookie.path or "/",
        }
    )


def _set_date(browser, name, value):
    element = browser.find_element(By.NAME, name)
    browser.execute_script(
        "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
        element,
        value,
    )


@pytest.mark.ui
def test_user_submits_report_and_sees_match(browser, live_server):
    _login_browser(browser, live_server)
    wait = WebDriverWait(browser, 10)

    browser.get(f"{live_server}/report")
    wait.until(ec.visibility_of_element_located((By.NAME, "title"))).send_keys(
        "AirPods Pro 不見了"
    )
    browser.find_element(By.NAME, "category").send_keys("電子產品")
    browser.find_element(By.NAME, "location").send_keys("二活三樓")
    _set_date(browser, "lost_date_start", "2026-06-07")
    _set_date(browser, "lost_date_end", "2026-06-07")
    browser.find_element(By.NAME, "description").send_keys(
        "白色耳機盒，灰色保護套"
    )
    browser.find_element(By.CSS_SELECTOR, "form.form-grid button[type='submit']").click()

    wait.until(ec.url_contains("/matches"))
    page = browser.find_element(By.TAG_NAME, "body").text
    assert "AirPods Pro 不見了" in page
    assert "白色 AirPods Pro 耳機" in page
    assert "83%" in page
    assert "FB交流版" in page


@pytest.mark.ui
def test_guest_report_draft_survives_login_redirect(browser, live_server):
    wait = WebDriverWait(browser, 10)

    browser.get(f"{live_server}/report")
    wait.until(ec.visibility_of_element_located((By.NAME, "title"))).send_keys(
        "黑色 iPhone 15 手機"
    )
    browser.find_element(By.NAME, "category").send_keys("電子產品")
    browser.find_element(By.NAME, "location").send_keys("新生南路校門")
    _set_date(browser, "lost_date_start", "2026-06-07")
    _set_date(browser, "lost_date_end", "2026-06-07")
    browser.find_element(By.NAME, "description").send_keys("透明保護殼，背面有藍色貼紙")

    submit = browser.find_element(By.CSS_SELECTOR, "form.form-grid button[type='submit']")
    assert submit.get_attribute("disabled") == "true"
    browser.find_element(By.CSS_SELECTOR, "[data-preserve-report]").click()

    wait.until(ec.url_contains("/login"))
    assert "next=/report" in browser.current_url

    _login_browser(browser, live_server)
    browser.get(f"{live_server}/report")

    assert wait.until(ec.visibility_of_element_located((By.NAME, "title"))).get_attribute("value") == "黑色 iPhone 15 手機"
    assert browser.find_element(By.NAME, "category").get_attribute("value") == "電子產品"
    assert browser.find_element(By.NAME, "location").get_attribute("value") == "新生南路校門"
    assert browser.find_element(By.NAME, "lost_date_start").get_attribute("value") == "2026-06-07"
    assert browser.find_element(By.NAME, "lost_date_end").get_attribute("value") == "2026-06-07"
    assert browser.find_element(By.NAME, "description").get_attribute("value") == "透明保護殼，背面有藍色貼紙"
