from __future__ import annotations

import json
import os
import smtplib
from contextlib import closing
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from flask import Flask, request, session, render_template, redirect, url_for, flash
from dotenv import load_dotenv

import matching
import bridge
from frontend_items import FRONTEND_ITEMS

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
# Vercel 等 serverless 環境檔案系統唯讀，可用 MAIL_LOG_PATH 指到 /tmp（沒設定 SMTP 時才會用到）。
MAIL_LOG = Path(os.environ.get("MAIL_LOG_PATH", str(BASE_DIR / "mail.log")))

DATABASE_URL = os.environ.get("DATABASE_URL")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")

from supabase import create_client, Client
supabase: Client | None = create_client(SUPABASE_URL, SUPABASE_ANON_KEY) if SUPABASE_URL and SUPABASE_ANON_KEY else None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-for-production")
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=1)

EMBED_BATCH = 32  # 一次送進 Jina 的招領物筆數


# --- Template Filters ---
@app.template_filter('format_time')
def format_time(s):
    if not s: return ""
    if isinstance(s, datetime):
        return s.strftime("%Y/%m/%d %H:%M")
    try:
        return datetime.fromisoformat(str(s)).strftime("%Y/%m/%d %H:%M")
    except ValueError:
        return s

@app.template_filter('from_json')
def from_json(s):
    try:
        return json.loads(s)
    except Exception:
        return []

@app.context_processor
def inject_globals():
    # 通報表單與篩選共用同一組正規類別，確保選項一致。
    return {"categories": matching.CANONICAL_CATEGORIES}


# --- Database ---
def get_db() -> psycopg.Connection:
    """開一條 Supabase Postgres 連線（dict row）。

    prepare_threshold=None 是為了配合 Supabase 的 transaction pooler（pgbouncer），
    避免 prepared statement 在連線間衝突。
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 未設定")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def is_ntu_email(email: str) -> bool:
    return email.strip().lower().endswith("@ntu.edu.tw")

def safe_next_url(value: str | None) -> str | None:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None

# 應用自有資料表（找到的招領物 lost_items 由爬蟲 / SQLAlchemy 那側維護）。
_SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS users (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        supabase_id text UNIQUE,
        name text NOT NULL,
        email text NOT NULL UNIQUE,
        created_at text NOT NULL
    )""",
    # lost_date_start / lost_date_end 為日期區間（YYYY-MM-DD）；使用者通常記不清確切時間，
    # 且圖書館資料只有日期，故以「天」為精度的區間取代原本精確到分鐘的單一時刻。
    # status：open（進行中）/ resolved（已找到）。
    """CREATE TABLE IF NOT EXISTS lost_reports (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES users(id),
        title text NOT NULL,
        category text,
        location text,
        lost_date_start text,
        lost_date_end text,
        status text NOT NULL DEFAULT 'open',
        description text,
        embedding text,
        created_at text NOT NULL
    )""",
    # lost_item_id 對應 lost_items.id（integer），型別需相符。
    """CREATE TABLE IF NOT EXISTS matches (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        report_id bigint NOT NULL REFERENCES lost_reports(id),
        lost_item_id integer NOT NULL REFERENCES lost_items(id),
        score int NOT NULL,
        reasons_json text NOT NULL,
        created_at text NOT NULL,
        UNIQUE(report_id, lost_item_id)
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES users(id),
        subject text NOT NULL,
        message text NOT NULL,
        is_read int NOT NULL DEFAULT 0,
        delivery text NOT NULL DEFAULT 'email',
        created_at text NOT NULL
    )""",
    # 招領物的語意向量（JSON 陣列存 text；資料量小，於 Python 端算 cosine）。
    "ALTER TABLE lost_items ADD COLUMN IF NOT EXISTS embedding text",
    # --- 既有資料庫的欄位遷移（CREATE TABLE IF NOT EXISTS 不會改既有表）---
    "ALTER TABLE lost_reports ADD COLUMN IF NOT EXISTS lost_date_start text",
    "ALTER TABLE lost_reports ADD COLUMN IF NOT EXISTS lost_date_end text",
    "ALTER TABLE lost_reports ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'open'",
    # 舊的精確到分鐘的單一時刻欄位不再使用（無正式資料）。
    "ALTER TABLE lost_reports DROP COLUMN IF EXISTS lost_at",
]

def init_db() -> None:
    with closing(get_db()) as db:
        for statement in _SCHEMA_STATEMENTS:
            db.execute(statement)
        db.commit()

# 注意：schema 由 `task setup-supabase`（scripts/setup_db.py）一次建立，
# 不在 import 時自動執行 DDL —— 這樣 Vercel 每次 cold start 才不會多打一次資料庫。


# --- Email ---
def send_email(recipient: str, subject: str, body: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    smtp_sender = os.environ.get("SMTP_SENDER", smtp_user or "noreply@ntu-lost-etl.local")
    if not smtp_host or not smtp_user or not smtp_password:
        try:
            with MAIL_LOG.open("a", encoding="utf-8") as handle:
                handle.write(f"[{now_iso()}] TO: {recipient}\nSUBJECT: {subject}\n{body}\n\n")
        except OSError:
            app.logger.info("SMTP 未設定，且 mail.log 不可寫入；略過信件：%s", subject)
        return False
    message = EmailMessage()
    message["From"] = smtp_sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    # 465 = implicit SSL (SMTPS)；587/其他 = STARTTLS。
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    return True


# --- Auth Helpers ---
def require_login() -> int | None:
    return session.get("user_id")

def get_user(user_id: int):
    with closing(get_db()) as db:
        return db.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()


# --- Found items (lost_items) <-> 前端 / 媒合用的 external 形狀 ---
def _external_from_lost_item(row: dict) -> dict:
    """把一筆 lost_items 列映射成前端 / 媒合用的 dict，並帶上 id。"""
    external = bridge.lost_item_to_external(row)
    external["id"] = row["id"]
    return external

def fetch_bundle(user_id: int | None) -> dict:
    with closing(get_db()) as db:
        item_rows = db.execute("SELECT * FROM lost_items ORDER BY found_date DESC").fetchall()
        external_items = [_external_from_lost_item(r) for r in item_rows]
        external_items.extend(dict(item) for item in FRONTEND_ITEMS)
        external_items.sort(key=lambda item: item["found_at"], reverse=True)
        source_locks = {"facebook": any(e["source_type"] == "facebook" for e in external_items)}
        if not user_id:
            external_items = [e for e in external_items if e["source_type"] != "facebook"]
            return {
                "external_items": external_items,
                "reports": [],
                "matches": [],
                "notifications": [],
                "source_locks": source_locks,
            }

        reports = [dict(r) for r in db.execute("SELECT * FROM lost_reports WHERE user_id = %s ORDER BY created_at DESC", (user_id,))]
        match_rows = db.execute(
            """SELECT m.id, m.score, m.reasons_json, m.created_at, r.title AS report_title,
                      li.source_system, li.original_id, li.found_date, li.location,
                      li.description, li.category, li.storage_place
               FROM matches m
               JOIN lost_reports r ON r.id = m.report_id
               JOIN lost_items li ON li.id = m.lost_item_id
               WHERE r.user_id = %s
               ORDER BY m.score DESC, m.created_at DESC""",
            (user_id,),
        ).fetchall()
        matches = []
        for mr in match_rows:
            ext = bridge.lost_item_to_external(mr)
            matches.append({
                "id": mr["id"], "score": mr["score"], "reasons_json": mr["reasons_json"], "created_at": mr["created_at"],
                "report_title": mr["report_title"],
                "external_title": ext["title"], "external_location": ext["location"],
                "external_source_name": ext["source_name"], "external_source_type": ext["source_type"],
                "external_source_url": ext["source_url"],
            })
        notifications = [dict(r) for r in db.execute("SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC", (user_id,))]
    return {
        "external_items": external_items,
        "reports": reports,
        "matches": matches,
        "notifications": notifications,
        "source_locks": {"facebook": False},
    }


# --- Embeddings (JSON text 欄位 + Python cosine) ---
def _ensure_report_embedding(db, report) -> list[float] | None:
    if report.get("embedding"):
        try:
            return json.loads(report["embedding"])
        except (TypeError, ValueError):
            pass
    if not matching.embeddings_enabled():
        return None
    try:
        vec = matching.embed_text(matching.item_text(report))
        db.execute("UPDATE lost_reports SET embedding = %s WHERE id = %s", (json.dumps(vec), report["id"]))
        db.commit()
        return vec
    except Exception:
        app.logger.exception("產生通報 embedding 失敗，改用關鍵字比對")
        return None

def _ensure_item_embedding(db, item_row, external) -> list[float] | None:
    if item_row.get("embedding"):
        try:
            return json.loads(item_row["embedding"])
        except (TypeError, ValueError):
            pass
    if not matching.embeddings_enabled():
        return None
    try:
        vec = matching.embed_text(matching.item_text(external))
        db.execute("UPDATE lost_items SET embedding = %s WHERE id = %s", (json.dumps(vec), item_row["id"]))
        db.commit()
        return vec
    except Exception:
        app.logger.exception("產生招領物 embedding 失敗，改用關鍵字比對")
        return None

def _ensure_all_item_embeddings(db) -> None:
    """為尚未產生向量的招領物批次補算 embedding。"""
    rows = db.execute("SELECT * FROM lost_items WHERE embedding IS NULL OR embedding = ''").fetchall()
    if not rows:
        return
    for start in range(0, len(rows), EMBED_BATCH):
        chunk = rows[start:start + EMBED_BATCH]
        texts = [matching.item_text(_external_from_lost_item(r)) for r in chunk]
        vectors = matching.embed_texts(texts)
        for row, vec in zip(chunk, vectors):
            db.execute("UPDATE lost_items SET embedding = %s WHERE id = %s", (json.dumps(vec), row["id"]))
    db.commit()

def _item_cosines(db, report_embedding: list[float]) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in db.execute("SELECT id, embedding FROM lost_items WHERE embedding IS NOT NULL AND embedding <> ''"):
        try:
            vec = json.loads(row["embedding"])
        except (TypeError, ValueError):
            continue
        result[row["id"]] = matching.cosine(report_embedding, vec)
    return result


# --- Matching ---
def _try_create_match(db, report, external, cos: float | None) -> bool:
    score, reasons = matching.blended_score(report, external, cos)
    if score < matching.MATCH_THRESHOLD:
        return False
    exists = db.execute("SELECT id FROM matches WHERE report_id = %s AND lost_item_id = %s", (report["id"], external["id"])).fetchone()
    if exists:
        return False
    db.execute(
        "INSERT INTO matches (report_id, lost_item_id, score, reasons_json, created_at) VALUES (%s, %s, %s, %s, %s)",
        (report["id"], external["id"], score, json.dumps(reasons, ensure_ascii=False), now_iso()),
    )
    db.commit()
    return True

# 與 UI（app.html 通知紀錄標題）一致的提醒：媒合只是資訊比對，認領仍須回原單位。
MATCH_DISCLAIMER = "提醒：本平台僅提供資訊媒合，實際認領請到原公告單位辦理流程。"


def _match_line(external) -> str:
    line = f"・{external['title']}（來源：{external['source_name']}，地點：{external['location']}）"
    if external["source_type"] == "facebook":
        line += f"\n  原始來源連結：{external['source_url']}"
    return line


def _build_match_email(pairs) -> tuple[str, str]:
    """把同一使用者的多筆新配對 (report, external) 彙整成一封信的 (subject, body)。"""
    # 依通報分組，同一份通報的多個招領物列在同一段落。
    by_report: dict = {}
    order: list = []
    for report, external in pairs:
        rid = report["id"]
        if rid not in by_report:
            by_report[rid] = (report, [])
            order.append(rid)
        by_report[rid][1].append(external)

    if len(pairs) == 1:
        subject = f"新的遺失物媒合結果：{pairs[0][0]['title']}"
    else:
        subject = f"新的遺失物媒合結果（共 {len(pairs)} 筆）"

    sections = []
    for rid in order:
        report, externals = by_report[rid]
        lines = [f"你的遺失通報「{report['title']}」出現以下可能配對："]
        lines.extend(_match_line(ext) for ext in externals)
        sections.append("\n".join(lines))
    body = "\n\n".join(sections) + "\n\n" + MATCH_DISCLAIMER
    return subject, body


def _notify_user_matches(db, user, pairs) -> None:
    """為單一使用者的所有新配對記錄站內通知，並寄出「一封」彙整信。

    pairs：list[(report, external)]，皆屬於同一個 user。
    """
    if not pairs:
        return
    # 站內通知維持逐筆，方便使用者在列表逐項檢視。
    for report, external in pairs:
        message = f"你的遺失通報「{report['title']}」出現新的可能配對：{external['title']}（來源：{external['source_name']}，地點：{external['location']}）。"
        subject = f"新的遺失物媒合結果：{report['title']}"
        db.execute(
            "INSERT INTO notifications (user_id, subject, message, delivery, created_at) VALUES (%s, %s, %s, 'email', %s)",
            (user["id"], subject, message, now_iso()),
        )
    db.commit()
    # Email 則彙整成一封，避免同時多筆配對時連發多封信。
    subject, body = _build_match_email(pairs)
    send_email(user["email"], subject, body)

def run_matching(report_id: int) -> None:
    """新通報 → 比對所有招領物（lost_items），對新配對發出通知。"""
    with closing(get_db()) as db:
        report = db.execute("SELECT * FROM lost_reports WHERE id = %s", (report_id,)).fetchone()
        if not report: return
        user = db.execute("SELECT * FROM users WHERE id = %s", (report["user_id"],)).fetchone()
        report_embedding = _ensure_report_embedding(db, report)
        cosine_by_item = None
        if report_embedding is not None:
            try:
                _ensure_all_item_embeddings(db)
                cosine_by_item = _item_cosines(db, report_embedding)
            except Exception:
                app.logger.exception("語意媒合失敗，改用關鍵字比對")
        new_pairs = []
        for item_row in db.execute("SELECT * FROM lost_items").fetchall():
            external = _external_from_lost_item(item_row)
            cos = cosine_by_item.get(item_row["id"]) if cosine_by_item is not None else None
            if _try_create_match(db, report, external, cos):
                new_pairs.append((report, external))
        # 同一份通報可能同時對上多筆招領物 → 彙整成一封信。
        _notify_user_matches(db, user, new_pairs)

def run_matching_for_lost_item(lost_item_id: int) -> list[tuple]:
    """單筆招領物 → 反向比對所有現有通報，建立新 match。

    回傳新配對的 (user, report, external) 清單；此處「不」寄信，交由呼叫端
    （process_new_lost_items）跨多筆招領物依使用者彙整後再一次寄出，避免連發多封。
    """
    new_pairs: list[tuple] = []
    with closing(get_db()) as db:
        item_row = db.execute("SELECT * FROM lost_items WHERE id = %s", (lost_item_id,)).fetchone()
        if not item_row: return []
        external = _external_from_lost_item(item_row)
        item_embedding = _ensure_item_embedding(db, item_row, external)
        # 只比對「進行中」的通報——已標記找到的不再媒合 / 通知。
        for report in db.execute("SELECT * FROM lost_reports WHERE status = 'open'").fetchall():
            cos = None
            if item_embedding is not None:
                report_embedding = _ensure_report_embedding(db, report)
                if report_embedding is not None:
                    cos = matching.cosine(item_embedding, report_embedding)
            if _try_create_match(db, report, external, cos):
                user = db.execute("SELECT * FROM users WHERE id = %s", (report["user_id"],)).fetchone()
                new_pairs.append((user, report, external))
    return new_pairs

def process_new_lost_items() -> dict:
    """處理「剛爬進來、還沒算過向量」的招領物：產生 embedding 並反向比對現有通報。

    爬蟲（scripts/scrapers/supa_crawl_lib.py）只負責把資料 upsert 進 lost_items；
    這支負責後續的語意處理與媒合，適合在每次爬完後執行（task match-lostitems）。
    """
    with closing(get_db()) as db:
        new_ids = [r["id"] for r in db.execute("SELECT id FROM lost_items WHERE embedding IS NULL OR embedding = '' ORDER BY id")]
    # 先跨所有新招領物收集配對，再依使用者彙整 → 一個人一封信。
    all_new: list[tuple] = []
    for lost_item_id in new_ids:
        all_new.extend(run_matching_for_lost_item(lost_item_id))
    if all_new:
        with closing(get_db()) as db:
            by_user: dict = {}
            for user, report, external in all_new:
                if user["id"] not in by_user:
                    by_user[user["id"]] = (user, [])
                by_user[user["id"]][1].append((report, external))
            for user, pairs in by_user.values():
                _notify_user_matches(db, user, pairs)
    return {"ok": True, "processed": len(new_ids), "new_matches": len(all_new)}


# --- Routes ---
@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next_url(request.args.get("next"))
    if next_url:
        session["next_url"] = next_url

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not is_ntu_email(email):
            flash("請輸入正確的台大 Email 格式 (必須以 @ntu.edu.tw 結尾)", "error")
            return redirect(url_for("login"))
        if not supabase:
            flash("系統未設定 Supabase 憑證。", "error")
            return redirect(url_for("login"))
        try:
            supabase.auth.sign_in_with_otp({"email": email})
            session["auth_email"] = email
            session["auth_time"] = datetime.now().timestamp()
            flash("驗證碼已寄出，請檢查您的台大信箱。", "info")
            return redirect(url_for("verify"))
        except Exception:
            app.logger.exception("Failed to send Supabase OTP")
            flash("發送驗證碼失敗，請稍後再試。", "error")
            return redirect(url_for("login"))

    # Clear stale auth state when visiting login page
    session.pop("auth_email", None)
    session.pop("auth_time", None)
    return render_template("auth.html", step="email")

@app.route("/verify", methods=["GET", "POST"])
def verify():
    email = session.get("auth_email")
    auth_time = session.get("auth_time")

    # Block if no email, or if the OTP request is older than 10 minutes (600 seconds)
    if not email or not auth_time or (datetime.now().timestamp() - auth_time > 600):
        session.pop("auth_email", None)
        session.pop("auth_time", None)
        flash("驗證已超時或無效，請重新輸入 Email。", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        if not otp:
            flash("請輸入驗證碼。", "error")
            return redirect(url_for("verify"))
        if not supabase:
            flash("系統未設定 Supabase 憑證。", "error")
            return redirect(url_for("login"))
        try:
            res = supabase.auth.verify_otp({"email": email, "token": otp, "type": "email"})
            if res and res.user:
                sb_user = res.user
                with closing(get_db()) as db:
                    user = db.execute(
                        "SELECT id, supabase_id FROM users WHERE supabase_id = %s OR email = %s",
                        (sb_user.id, email),
                    ).fetchone()
                    if not user:
                        name = email.split("@")[0]
                        row = db.execute(
                            "INSERT INTO users (supabase_id, name, email, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                            (sb_user.id, name, email, now_iso()),
                        ).fetchone()
                        db.commit()
                        user = {"id": row["id"], "supabase_id": sb_user.id}
                    elif not user["supabase_id"]:
                        db.execute("UPDATE users SET supabase_id = %s WHERE id = %s", (sb_user.id, user["id"]))
                        db.commit()
                session["user_id"] = user["id"]
                session.pop("auth_email", None)
                session.pop("auth_time", None)
                session.permanent = True
                next_url = session.pop("next_url", None)
                return redirect(next_url or url_for("dashboard"))
        except Exception:
            flash("驗證失敗: 驗證碼錯誤或已過期。", "error")
    return render_template("auth.html", step="otp", email=email)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def dashboard():
    user_id = session.get("user_id")
    user = get_user(user_id) if user_id else None
    bundle = fetch_bundle(user_id)
    summary = {}
    for item in bundle["external_items"]:
        summary[item["source_name"]] = summary.get(item["source_name"], 0) + 1
    return render_template("app.html", view="dashboard", user=user, summary=summary, **bundle)

@app.route("/sources")
def sources():
    user_id = session.get("user_id")
    user = get_user(user_id) if user_id else None
    bundle = fetch_bundle(user_id)
    q = request.args.get("q", "").strip().lower()
    source = request.args.get("source", "all")
    category = request.args.get("category", "all")
    filtered = []
    for item in bundle["external_items"]:
        text = f"{item['title']} {item['location']} {item['description']} {item['source_name']} {item['category']}".lower()
        if q and q not in text: continue
        if source != "all" and item["source_name"] != source: continue
        if category != "all" and item["category"] != category: continue
        filtered.append(item)
    bundle.pop("external_items", None)
    return render_template("app.html", view="sources", user=user, external_items=filtered, q=q, cur_source=source, cur_cat=category, **bundle)

def _read_report_form():
    """讀取並驗證通報表單；回傳欄位 dict，不完整則回傳 None。"""
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "").strip()
    location = request.form.get("location", "").strip()
    start = request.form.get("lost_date_start", "").strip()
    end = request.form.get("lost_date_end", "").strip() or start  # 沒填結束日就視為單日
    description = request.form.get("description", "").strip()
    if not all([title, category, location, start, description]):
        return None
    if end < start:
        start, end = end, start
    return {"title": title, "category": category, "location": location,
            "lost_date_start": start, "lost_date_end": end, "description": description}

def _owned_report(db, rid: int, user_id: int):
    return db.execute("SELECT * FROM lost_reports WHERE id = %s AND user_id = %s", (rid, user_id)).fetchone()

@app.route("/report", methods=["GET", "POST"])
def report():
    user_id = require_login()
    user = get_user(user_id) if user_id else None
    if request.method == "POST":
        if not user_id:
            flash("請先登入，登入後會回到登記頁繼續完成送出。", "error")
            return redirect(url_for("login", next=url_for("report")))
        data = _read_report_form()
        if data:
            with closing(get_db()) as db:
                row = db.execute(
                    "INSERT INTO lost_reports (user_id, title, category, location, lost_date_start, lost_date_end, description, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (user_id, data["title"], data["category"], data["location"], data["lost_date_start"], data["lost_date_end"], data["description"], now_iso()),
                ).fetchone()
                db.commit()
                report_id = row["id"]
            run_matching(report_id)
            flash("通報已成功送出！", "info")
            return redirect(url_for("matches"))
        flash("請完整填寫通報資訊。", "error")
    return render_template("app.html", view="report", user=user, **fetch_bundle(user_id))

@app.route("/report/<int:rid>/edit", methods=["GET", "POST"])
def edit_report(rid):
    user_id = require_login()
    if not user_id: return redirect(url_for("login"))
    user = get_user(user_id)
    with closing(get_db()) as db:
        rep = _owned_report(db, rid, user_id)
    if not rep:
        flash("找不到該通報。", "error")
        return redirect(url_for("report"))
    if request.method == "POST":
        data = _read_report_form()
        if data:
            with closing(get_db()) as db:
                # 內容改變 → 清掉舊 embedding 與舊媒合、狀態回到進行中，再重新比對。
                db.execute("DELETE FROM matches WHERE report_id = %s", (rid,))
                db.execute(
                    "UPDATE lost_reports SET title=%s, category=%s, location=%s, lost_date_start=%s, lost_date_end=%s, description=%s, embedding=NULL, status='open' WHERE id=%s AND user_id=%s",
                    (data["title"], data["category"], data["location"], data["lost_date_start"], data["lost_date_end"], data["description"], rid, user_id),
                )
                db.commit()
            run_matching(rid)
            flash("通報已更新並重新比對。", "info")
            return redirect(url_for("matches"))
        flash("請完整填寫通報資訊。", "error")
    return render_template("app.html", view="report", user=user, edit_report=dict(rep), **fetch_bundle(user_id))

@app.route("/report/<int:rid>/resolve", methods=["POST"])
def resolve_report(rid):
    user_id = require_login()
    if user_id:
        with closing(get_db()) as db:
            db.execute("UPDATE lost_reports SET status='resolved' WHERE id=%s AND user_id=%s", (rid, user_id))
            db.commit()
        flash("已標記為找到，將不再為此通報媒合。", "info")
    return redirect(url_for("report"))

@app.route("/report/<int:rid>/reopen", methods=["POST"])
def reopen_report(rid):
    user_id = require_login()
    if user_id:
        with closing(get_db()) as db:
            db.execute("UPDATE lost_reports SET status='open' WHERE id=%s AND user_id=%s", (rid, user_id))
            db.commit()
        flash("已重新開啟通報。", "info")
    return redirect(url_for("report"))

@app.route("/report/<int:rid>/delete", methods=["POST"])
def delete_report(rid):
    user_id = require_login()
    if user_id:
        with closing(get_db()) as db:
            if _owned_report(db, rid, user_id):
                db.execute("DELETE FROM matches WHERE report_id = %s", (rid,))
                db.execute("DELETE FROM lost_reports WHERE id=%s AND user_id=%s", (rid, user_id))
                db.commit()
                flash("通報已刪除。", "info")
    return redirect(url_for("report"))

@app.route("/matches")
def matches():
    user_id = require_login()
    if not user_id: return redirect(url_for("login"))
    return render_template("app.html", view="matches", user=get_user(user_id), **fetch_bundle(user_id))

@app.route("/notifications")
def notifications():
    user_id = require_login()
    if not user_id: return redirect(url_for("login"))
    return render_template("app.html", view="notifications", user=get_user(user_id), **fetch_bundle(user_id))

@app.route("/notifications/read-all", methods=["POST"])
def read_all_notifications():
    user_id = require_login()
    if user_id:
        with closing(get_db()) as db:
            db.execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s", (user_id,))
            db.commit()
    return redirect(url_for("notifications"))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
