from __future__ import annotations

from copy import deepcopy

import pytest

import app as webapp


@pytest.mark.integration
def test_dashboard_renders_guest_summary(client, monkeypatch, frontend_bundle):
    guest_bundle = deepcopy(frontend_bundle)
    guest_bundle["external_items"] = [
        item for item in guest_bundle["external_items"] if item["source_type"] != "facebook"
    ]
    monkeypatch.setattr(webapp, "fetch_bundle", lambda user_id: deepcopy(guest_bundle))

    response = client.get("/")

    assert response.status_code == 200
    assert "訪客模式" in response.text
    assert "駐警隊" in response.text
    assert "請登入以查看" in response.text
    assert 'href="/report"' in response.text
    assert "白色 AirPods Pro 耳機" not in response.text


@pytest.mark.integration
def test_sources_filters_by_source_and_query(client, monkeypatch, frontend_bundle):
    monkeypatch.setattr(webapp, "fetch_bundle", lambda user_id: deepcopy(frontend_bundle))

    response = client.get("/sources?source=駐警隊&q=iPhone")

    assert response.status_code == 200
    assert "黑色 iPhone 15 手機" in response.text
    assert "深咖啡色皮夾" not in response.text
    assert "白色 AirPods Pro 耳機" not in response.text


@pytest.mark.integration
def test_sources_show_locked_facebook_prompt_for_guests(client, monkeypatch, frontend_bundle):
    guest_bundle = deepcopy(frontend_bundle)
    guest_bundle["external_items"] = [
        item for item in guest_bundle["external_items"] if item["source_type"] != "facebook"
    ]
    monkeypatch.setattr(webapp, "fetch_bundle", lambda user_id: deepcopy(guest_bundle))

    response = client.get("/sources?source=FB交流版")

    assert response.status_code == 200
    assert "FB交流版內容限台大學生登入後查看。請登入以查看。" in response.text
    assert "白色 AirPods Pro 耳機" not in response.text


@pytest.mark.integration
def test_guest_can_open_report_page_but_cannot_submit(client, monkeypatch, frontend_bundle):
    guest_bundle = deepcopy(frontend_bundle)
    guest_bundle["external_items"] = [
        item for item in guest_bundle["external_items"] if item["source_type"] != "facebook"
    ]
    monkeypatch.setattr(webapp, "fetch_bundle", lambda user_id: deepcopy(guest_bundle))

    response = client.get("/report")

    assert response.status_code == 200
    assert "請先登入才能送出登記" in response.text
    assert "先登入再送出" in response.text

    response = client.post(
        "/report",
        data={
            "title": "AirPods Pro 不見了",
            "category": "電子產品",
            "location": "二活三樓",
            "lost_date_start": "2026-06-07",
            "lost_date_end": "2026-06-07",
            "description": "白色耳機盒，灰色保護套",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login?next=/report")


@pytest.mark.integration
def test_login_rejects_non_ntu_email_without_calling_supabase(client, monkeypatch):
    class SupabaseMustNotBeCalled:
        @property
        def auth(self):
            raise AssertionError("Supabase should not be called for an invalid email")

    monkeypatch.setattr(webapp, "supabase", SupabaseMustNotBeCalled())

    response = client.post(
        "/login",
        data={"email": "student@gmail.com"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "必須以 @ntu.edu.tw 結尾" in response.text


class InsertResult:
    def fetchone(self):
        return {"id": 42}


class InsertConnection:
    def __init__(self):
        self.params = None
        self.committed = False

    def execute(self, query, params=()):
        assert "INSERT INTO lost_reports" in query
        self.params = params
        return InsertResult()

    def commit(self):
        self.committed = True

    def close(self):
        pass


class ExistingUserResult:
    def fetchone(self):
        return {"id": 7, "supabase_id": "supabase-user-id"}


class ExistingUserConnection:
    def execute(self, query, params=()):
        assert "SELECT id, supabase_id FROM users" in query
        return ExistingUserResult()

    def commit(self):
        pass

    def close(self):
        pass


class FakeSupabaseUser:
    id = "supabase-user-id"


class VerifyOtpResult:
    user = FakeSupabaseUser()


class FakeSupabaseAuth:
    def sign_in_with_otp(self, payload):
        assert payload == {"email": "student@ntu.edu.tw"}

    def verify_otp(self, payload):
        assert payload["email"] == "student@ntu.edu.tw"
        assert payload["token"] == "123456"
        assert payload["type"] == "email"
        return VerifyOtpResult()


class FakeSupabase:
    auth = FakeSupabaseAuth()


@pytest.mark.integration
def test_login_returns_to_next_url_after_otp_verification(client, monkeypatch):
    monkeypatch.setattr(webapp, "supabase", FakeSupabase())
    monkeypatch.setattr(webapp, "get_db", ExistingUserConnection)

    response = client.get("/login?next=/report")
    assert response.status_code == 200

    response = client.post("/login", data={"email": "student@ntu.edu.tw"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/verify")

    response = client.post("/verify", data={"otp": "123456"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/report")


@pytest.mark.integration
def test_report_post_creates_report_and_triggers_matching(client, monkeypatch):
    connection = InsertConnection()
    matched_report_ids = []
    monkeypatch.setattr(webapp, "get_db", lambda: connection)
    monkeypatch.setattr(
        webapp,
        "get_user",
        lambda user_id: {"id": user_id, "email": "student@ntu.edu.tw"},
    )
    monkeypatch.setattr(webapp, "run_matching", matched_report_ids.append)

    with client.session_transaction() as session:
        session["user_id"] = 7

    response = client.post(
        "/report",
        data={
            "title": "AirPods Pro 不見了",
            "category": "電子產品",
            "location": "二活三樓",
            "lost_date_start": "2026-06-07",
            "lost_date_end": "2026-06-07",
            "description": "白色耳機盒，灰色保護套",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/matches")
    assert connection.committed is True
    assert connection.params[0] == 7
    assert connection.params[1] == "AirPods Pro 不見了"
    assert matched_report_ids == [42]
