-- ============================================================================
-- 應用自有資料表（Supabase Postgres）。app.init_db() 會在啟動時 idempotent 建立，
-- 這份檔案是給人看 / 手動在 SQL Editor 重建用的參考。
--
-- 招領物 lost_items 由爬蟲（scripts/scrapers/supa_crawl_lib.py）與 core/ 的
-- SQLAlchemy 模型維護；這裡只多加一個 embedding 欄位給語意媒合用。
--
-- 注意：lost_items.id 是 integer（SERIAL），所以 matches.lost_item_id 也用 integer。
-- ============================================================================

CREATE TABLE IF NOT EXISTS users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supabase_id text UNIQUE,
    name text NOT NULL,
    email text NOT NULL UNIQUE,
    created_at text NOT NULL
);

-- lost_date_start / lost_date_end 為遺失日期區間（YYYY-MM-DD，天精度）；使用者通常記不清
-- 確切時間、且圖書館資料只有日期，故以區間取代原本精確到分鐘的單一時刻（舊欄位 lost_at）。
-- status：open（進行中）/ resolved（已找到）。
CREATE TABLE IF NOT EXISTS lost_reports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    title text NOT NULL,
    category text,
    location text,
    lost_date_start text,
    lost_date_end text,
    status text NOT NULL DEFAULT 'open',
    description text,
    embedding text,          -- 語意向量（JSON 陣列字串）
    created_at text NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id bigint NOT NULL REFERENCES lost_reports(id),
    lost_item_id integer NOT NULL REFERENCES lost_items(id),
    score int NOT NULL,
    reasons_json text NOT NULL,
    created_at text NOT NULL,
    UNIQUE(report_id, lost_item_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(id),
    subject text NOT NULL,
    message text NOT NULL,
    is_read int NOT NULL DEFAULT 0,
    delivery text NOT NULL DEFAULT 'email',
    created_at text NOT NULL
);

-- 招領物語意向量（JSON 陣列存 text；資料量小，在 Python 端算 cosine）。
ALTER TABLE lost_items ADD COLUMN IF NOT EXISTS embedding text;

-- 之後資料量變大、要改用 pgvector 索引最近鄰查詢時，見 supabase/pgvector.sql。
