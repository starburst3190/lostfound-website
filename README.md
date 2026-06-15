# NTU 校園失物招領聚合平台

把台大各來源的「拾獲物」資料聚合起來，讓使用者只要提報「自己掉了什麼」，系統就用
**語意比對**找出可能的招領物並寄出通知。

- 來源資料由爬蟲匯入 Supabase Postgres 的 `lost_items`
- 使用者用台大信箱（`@ntu.edu.tw`）以 OTP 驗證碼登入
- 提報遺失物後，系統用 **Jina embeddings 語意相似度 + 類型/地點/時間** 混合評分媒合
- 命中時建立站內通知並寄 email

> 設計上只提報「遺失」、不開放上傳「拾獲」——拾獲資料一律由各來源爬蟲匯入。

## 架構總覽

```
圖書館等來源網站
      │  爬蟲（scripts/scrapers/supa_crawl_lib.py）
      ▼
Supabase Postgres：lost_items（招領物）
      │  embed + 反向媒合（scripts/match_lost_items.py）
      ▼
matches / notifications  ←─ 使用者提報（app.py 即時正向媒合）
      ▲
      │  Flask 伺服器渲染 UI（OTP 登入、來源名單、提報、媒合、通知）
   使用者
```

- **資料庫**：Supabase Postgres（透過 transaction pooler 連線）
- **認證**：Supabase Auth Email OTP
- **語意比對**：Jina embeddings v3（未設金鑰時自動退回關鍵字比對）
- **前端**：Flask + Jinja 伺服器端渲染

## 技術棧

| 範疇 | 使用技術 |
|------|----------|
| 語言 / 執行環境 | Python 3.12+ |
| Web 框架 | Flask 3 + Jinja2（伺服器端渲染） |
| 資料庫存取 | Supabase Postgres；psycopg 3（執行時直連）+ Flask-SQLAlchemy（建表 / 灌資料用 ORM） |
| 認證 | Supabase Auth（Email OTP），supabase-py 客戶端 |
| 語意比對 | Jina embeddings v3（text-matching，1024 維），cosine 於 Python 端計算 |
| 爬蟲 | requests + BeautifulSoup4 |
| Email 通知 | SMTP（標準庫 smtplib） |
| 設定管理 | python-dotenv |
| 套件管理 | Poetry（`pyproject.toml`）→ 匯出 `requirements.txt` 供部署 |
| 任務指令 | Task（`Taskfile.yml`） |
| 測試 | pytest（單元 / 整合）、Selenium（端對端） |
| 部署 | Vercel（`@vercel/python` WSGI）；另提供 Dockerfile 自架 |

## 專案結構

```text
lostfound-website/
├── app.py                  # Flask 網站（UI 路由 + 媒合引擎，psycopg 直連 Postgres）
├── matching.py             # 語意 + 結構化混合評分、地點簡稱正規化（純函式）
├── bridge.py               # 把 lost_items 列映射成 UI/媒合用的形狀
├── location_aliases.json   # 台大地點簡稱 → 正式名稱（可自行擴充）
├── category_rules.json     # 物品類型正規化規則（正規類別 → 關鍵字，可自行擴充）
├── core/                   # SQLAlchemy 模型 + app factory（給設定 / 匯入腳本用）
│   ├── __init__.py         #   create_app()（提供 DB context）
│   ├── extensions.py       #   db = SQLAlchemy()
│   └── models.py           #   LostItem（lost_items 表）
├── scripts/
│   ├── setup_db.py         # 一次建立所有資料表（task setup-supabase）
│   ├── seed_from_csv.py    # 選用：用 CSV 灌 lost_items
│   ├── match_lost_items.py # 對新爬到的招領物算向量 + 媒合（task match-lostitems）
│   └── scrapers/
│       ├── supa_crawl_lib.py        # 正式爬蟲 → lost_items（task crawl-lib）
│       └── lib_lostfound_scraper.py # 原型爬蟲 → CSV（開發用）
├── templates/              # auth.html / app.html / layout.html
├── static/styles.css
├── supabase/
│   ├── schema.sql          # 應用資料表（參考；實際由 setup_db 建立）
│   └── pgvector.sql        # 選用：之後升級成 pgvector 的步驟
├── api/index.py            # Vercel WSGI 進入點
├── vercel.json             # Vercel 部署設定
├── Dockerfile              # 自架容器（Vercel 以外的選項）
├── Taskfile.yml            # 指令集（開發 + 維運）
├── pyproject.toml          # 相依套件「來源」（Poetry）
└── requirements.txt        # 由 pyproject 匯出的部署用清單（Vercel/Docker 安裝它）
```

## 環境變數

複製 `.env.template` 成 `.env` 後填入：

| 變數 | 用途 | 必要性 |
|------|------|--------|
| `DATABASE_URL` | Supabase Postgres 連線字串（建議用 transaction pooler，port 6543） | 必填 |
| `SUPABASE_URL` | Supabase 專案 URL（OTP 登入） | 必填 |
| `SUPABASE_ANON_KEY` | Supabase anon key（OTP 登入） | 必填 |
| `SUPABASE_SERVICE_ROLE_KEY` | service role key（爬蟲寫入 / 建表 / storage） | 爬蟲與 setup 需要 |
| `SUPABASE_STORAGE_BUCKET` | 圖片 bucket 名稱（目前未存圖片，可留空） | 選填 |
| `JINA_API_KEY` | Jina embeddings 金鑰；留空則退回關鍵字比對 | 建議 |
| `SECRET_KEY` | Flask session 密鑰（**正式環境請換成隨機值**） | 正式必填 |
| `SMTP_HOST/PORT/USER/PASSWORD/SENDER` | email 通知；未設定則寫入 `mail.log` 不寄信 | 選填 |

> 連線字串等憑證的取得位置見下方「部署到 Vercel」。`.env` 已被 gitignore，不會進版控。

## 本機開發

```bash
# 1) 安裝相依套件（Poetry 為來源；沒有 Poetry 可改用 pip install -r requirements.txt）
task install

# 2) 建立資料表（只需做一次；idempotent）
task setup-supabase

# 3) 啟動網站
task up      # = cleanup + serve，預設 http://127.0.0.1:8000
```

其他：`task down`（停服務）、`task open`（開瀏覽器）。

## 資料管線（爬蟲 → 媒合）

```bash
task crawl-lib        # 爬圖書館失物 → 寫入 Supabase lost_items（7 天 watermark、去重）
task match-lostitems  # 對新招領物算 embedding，反向比對現有通報、必要時寄通知
```

## 部署到 Vercel（主要部署方式）

整個 Flask app 透過 `api/index.py`（WSGI）跑在 Vercel 上，`vercel.json` 會把所有路徑
導到它，Vercel 安裝 `requirements.txt`。

1. 在 Vercel `Add New Project`，選這個 GitHub repo（Framework Preset 選 `Other`）。
2. 在 Project → Settings → Environment Variables 填入上表變數
   （至少 `DATABASE_URL`、`SUPABASE_URL`、`SUPABASE_ANON_KEY`、`JINA_API_KEY`、`SECRET_KEY`、`SMTP_*`）。
3. 先建立資料表（擇一）：本機 `task setup-supabase`，或把 `supabase/schema.sql`
   貼到 Supabase SQL Editor 執行。
4. Deploy。

注意事項：

- `DATABASE_URL` 用 **transaction pooler**（serverless 友善，本專案已採用）。
- serverless 檔案系統唯讀；沒設 SMTP 時若要寫 `mail.log`，用環境變數 `MAIL_LOG_PATH=/tmp/mail.log`。
- 取得憑證：Supabase Dashboard →「Connect」拿 `DATABASE_URL`；Settings → API 拿
  `anon` / `service_role`；資料庫密碼在 Settings → Database。

### 自架（Vercel 以外）

`Dockerfile` 提供容器化部署（Render / Railway / Fly.io / 本地）：

```bash
docker build -t lostfound . && docker run -p 8000:8000 --env-file .env lostfound
```

## 媒合怎麼運作

評分以**物品本身（名稱 / 描述）的相似度為主訊號**，類型 / 地點 / 時間只是輔助。實作上刻意讓
「類型 + 地點 + 時間」的總分（`STRUCTURED_MAX`）**低於**媒合門檻（`MATCH_THRESHOLD`），
所以**光靠類型 + 地點 + 時間湊不到一筆媒合**——一定要名稱 / 語意夠相近才會成立。
（避免「同類型、同地點、同一天，但一個是雨傘、一個是耳機」被誤判。權重見 `matching.py` 開頭。）

- **語意（主訊號）**：把通報與招領物各自轉成向量，算 cosine 相似度（`錢` ≈ `現金`、
  `皮夾` ≈ `錢包`）；這是全場最大的單一權重，名稱夠接近時幾乎可單獨成立媒合。
- **結構化（輔助）**：類型一致、地點相近、時間吻合的加分，用來在「名稱已相近」的候選間
  排序與加強信心，但單靠它們跨不過門檻。
- **地點正規化**：`location_aliases.json` 把校內簡稱（`活大` → `第一學生活動中心`）展開後再比。
- **類型正規化**：各來源的雜亂類型（`其他 充電線`）與通報表單，都用 `category_rules.json`
  收斂成同一組正規類別，類型篩選與加分才對得上。
- **時間用區間**：通報以「遺失日期區間（最早～最晚，天精度）」比對拾獲日，符合使用者
  記不清確切時間、且圖書館資料只有日期的實況；落在區間內加最多分，鄰近數天遞減。
- **降級**：未設 `JINA_API_KEY` 時退回關鍵字重疊比對，服務仍可運作。

向量目前以 JSON 字串存在 text 欄位、在 Python 端算 cosine；資料量變大要改用
pgvector 索引時見 `supabase/pgvector.sql`。

實際的 HTTP 路由與資料結構見 [API_CONTRACT.md](API_CONTRACT.md)。
