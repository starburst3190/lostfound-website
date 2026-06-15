"""
Facebook 社團遺失物貼文擷取器
================================

透過 Chrome DevTools Protocol (CDP) 讀取無障礙樹 (Accessibility Tree)，
不需捲動即可解析畫面上已載入的 4~5 篇貼文，並可靠地反查每篇貼文的永久連結。

可靠抓取貼文網址的核心做法
--------------------------
Facebook 會把「時間戳記連結」的文字打亂 (anti-scraping)，所以舊版用
「比對時間文字 → 查字典拿網址」的方式幾乎一定失敗。

本版改成從貼文區塊內**任何**帶有貼文 ID 的連結反推 ID，再組成標準網址：
    - 相片連結   : photo/?fbid=...&set=gm.<POST_ID>
    - 檔案/文件   : /permalink/<POST_ID>/
    - 留言連結    : /posts/<POST_ID>/?comment_id=...
    - 其他        : multi_permalinks / story_fbid
拿到 POST_ID 後組成：
    https://www.facebook.com/groups/<group>/posts/<POST_ID>/
若整篇都找不到 ID（純文字貼文），則退而使用時間戳記連結的原始 (混淆) 網址，
那個網址在已登入的瀏覽器點下去仍會正確導向該貼文。

離線測試
--------
    python extractor.py --test old/debug_a11y_tree_dump.json
可在沒有瀏覽器、沒有 playwright 的情況下，直接用已存的無障礙樹驗證擷取邏輯。
"""

import time
import random
import re
import json
import argparse
from datetime import datetime, timedelta

# .env 為選用功能，缺少套件時不應讓整支程式崩潰
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ──────────────────────────────────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────────────────────────────────
CDP_ENDPOINT = "http://localhost:9222"   # 需先以 --remote-debugging-port=9222 啟動 Chrome

KEYWORDS = [
    "學生證", "身分證", "錢包", "耳機", "雨傘", "鑰匙", "水壺", "卡片",
    "遺失", "協尋", "拾獲", "不見", "忘記", "沒拿走", "撿到", "掉在",
    "放在", "拿給", "失物", "招領", "悠遊卡", "證件",
]

# 內文擷取會用到的標記文字角色
_TEXT_ROLES = ("StaticText", "link", "button", "heading", "InlineTextBox")

# 從各種網址格式反查「貼文 ID」的樣式
_POST_ID_PATTERNS = [
    re.compile(r"/posts/(\d+)"),
    re.compile(r"/permalink/(\d+)"),
    re.compile(r"set=gm\.(\d+)"),
    re.compile(r"multi_permalinks=(\d+)"),
    re.compile(r"story_fbid=(\d+)"),
]

# 貼文標頭/版面標記（繁中介面）
_ACTION_RE   = re.compile(r"貼文採取的動作")       # 「對○○的這則貼文採取的動作」(每篇貼文一顆 ⋯ 按鈕)
_AUDIENCE_RE = re.compile(r"^分享對象")            # 「分享對象：私密社團」
_TIME_RE     = re.compile(r"(\d+)\s*(分鐘|小時|天|週|個月|年)")
# 互動列 / 留言區起點（內文擷取碰到就停）
_ENGAGE_RE = re.compile(
    r"(讚|哈|哇|加油|大心|嗚|怒)[：:]|所有心情|則留言|則分享"
    r"|對.+的貼文(表示讚|傳達心情)|回應.+的貼文|^傳送$|^讚$|^留言$|^分享$"
    r"|查看更多回答|以虛擬替身|插入表情|附加相片|GIF\s*回應|貼圖回應|隱藏或檢舉"
)
# 純版面雜訊（內文擷取時略過，但不中止）
_CHROME_RE = re.compile(r"^查看更多$|^顯示更多$|^更多$|保持發言的成員|查看徽章詳情")

# ──────────────────────────────────────────────────────────────────────────
# 還原被混淆的「發文時間」
# ──────────────────────────────────────────────────────────────────────────
# Facebook 反爬蟲：把時間切成一堆「單字元 <span>」，真字元與假字元交錯，全都是
# 真實文字節點（無 aria-hidden），靠 CSS 把假字元藏起來、把真字元重新排序。
# 因此無障礙樹拿到的是亂碼。下面這段 JS 不移動滑鼠、不捲動，純粹用「版面」還原：
#   1. 走訪時間連結內所有文字節點
#   2. 丟掉被 CSS 隱藏的（display:none / visibility / opacity:0 / 寬高 0 / 跑到框外）
#   3. 把剩下的真字元依「螢幕座標」排序 → 重組出你眼睛看到的字串（例如「11小時」）
# 再用每篇貼文共用的 __cft__ token 把時間對應到 post_id（不需偵測貼文容器）。
_TIME_RECON_JS = r"""
() => {
  const cftRe = /__cft__\[0\]=([^&]+)/;
  const idRe = /\/posts\/(\d+)|\/permalink\/(\d+)|set=gm\.(\d+)|multi_permalinks=(\d+)|story_fbid=(\d+)/;
  const timeRe = /^(剛剛|剛才|\d+\s*(分鐘|分|小時|時|天|週|個月|年)前?|(\d{4}年)?\d{1,2}月\d{1,2}日[\s\d:週一二三四五六日上午下午凌晨晚]{0,12})$/;

  // __cft__ token -> post_id
  const cftToId = {};
  document.querySelectorAll('a[href]').forEach(a => {
    const h = a.href || '';
    const im = h.match(idRe);
    if (!im) return;
    const id = im.slice(1).find(Boolean);
    const cm = h.match(cftRe);
    if (cm && id && !cftToId[cm[1]]) cftToId[cm[1]] = id;
  });

  // 依版面還原連結內「看得到」的文字
  function visibleString(el) {
    const box = el.getBoundingClientRect();
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    const items = [];
    let node;
    while ((node = walker.nextNode())) {
      const txt = node.textContent;
      if (!txt || !txt.trim()) continue;
      const p = node.parentElement;
      if (!p) continue;
      const cs = getComputedStyle(p);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') continue;
      if (parseFloat(cs.opacity) === 0) continue;
      const r = p.getBoundingClientRect();
      if (r.width < 0.5 || r.height < 0.5) continue;            // font-size:0 / 寬高 0 的假字元
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      if (cx < box.left - 2 || cx > box.right + 2 ||             // 被移到框外的假字元
          cy < box.top - 2 || cy > box.bottom + 2) continue;
      items.push({ t: txt, x: r.left, y: r.top });
    }
    // 先依列（y）再依水平位置（x）排序 → 等同視覺由左到右、由上到下
    items.sort((a, b) => (Math.abs(a.y - b.y) > 4 ? a.y - b.y : a.x - b.x));
    return items.map(i => i.t).join('').replace(/\s+/g, '');
  }

  const out = {};
  document.querySelectorAll('a[href*="__cft__"]').forEach(a => {
    const cm = (a.href || '').match(cftRe);
    if (!cm) return;
    const id = cftToId[cm[1]];
    if (!id || out[id]) return;                 // 同一貼文取「文件順序最先」的時間連結（貼文本身在留言之前）
    const vt = visibleString(a);
    if (vt && vt.length <= 24 && timeRe.test(vt)) out[id] = vt;
  });
  return out;
}
"""


def is_rest_time(current_time, start_hour=22, end_hour=5):
    """判斷當前時間是否在休息時段內 (預設 22:00 ~ 05:00)"""
    if start_hour > end_hour:
        return current_time.hour >= start_hour or current_time.hour < end_hour
    return start_hour <= current_time.hour < end_hour


# ──────────────────────────────────────────────────────────────────────────
# CDP：擷取無障礙樹，並對每個 link 反查真實 href
# ──────────────────────────────────────────────────────────────────────────
def get_a11y_tree_cdp(page):
    print("[*] 正在透過 CDP 提取無障礙樹並執行深度 JS 反查網址...")
    client = page.context.new_cdp_session(page)
    try:
        client.send("DOM.enable")
        client.send("Runtime.enable")
        client.send("Accessibility.enable")

        ax_tree = client.send("Accessibility.getFullAXTree")
        client.send("Accessibility.disable")

        nodes = ax_tree.get("nodes", [])
        if not nodes:
            print("[DEBUG-警告] CDP 回傳的節點陣列為空！")
            return {}

        node_map = {n["nodeId"]: n for n in nodes}
        root_node = next(
            (n for n in nodes if n.get("role", {}).get("value") == "RootWebArea"),
            nodes[0],
        )

        def fetch_href_via_js(backend_id):
            if not backend_id:
                return ""
            obj_id = None
            try:
                res = client.send("DOM.resolveNode", {"backendNodeId": backend_id})
                obj_id = res.get("object", {}).get("objectId")
                if not obj_id:
                    return ""
                js_code = """
                function() {
                    let el = this.nodeType === 3 ? this.parentElement : this;
                    let a = el ? el.closest('a') : null;
                    return a ? a.href : '';
                }
                """
                eval_res = client.send("Runtime.callFunctionOn", {
                    "objectId": obj_id,
                    "functionDeclaration": js_code,
                    "returnByValue": True,
                })
                return eval_res.get("result", {}).get("value", "")
            except Exception:
                return ""
            finally:
                if obj_id:
                    try:
                        client.send("Runtime.releaseObject", {"objectId": obj_id})
                    except Exception:
                        pass

        def assemble_tree(node_id):
            raw_node = node_map.get(node_id)
            if not raw_node:
                return None
            role = raw_node.get("role", {}).get("value", "")
            name = raw_node.get("name", {}).get("value", "")
            url = ""
            if role == "link":
                url = fetch_href_via_js(raw_node.get("backendDOMNodeId"))
            children = []
            for child_id in raw_node.get("childIds", []):
                child_node = assemble_tree(child_id)
                if child_node:
                    children.append(child_node)
            return {"role": role, "name": name, "url": url, "children": children}

        return assemble_tree(root_node["nodeId"])
    finally:
        try:
            client.send("Runtime.disable")
            client.send("DOM.disable")
        except Exception:
            pass
        try:
            client.detach()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# 純函式：解析無障礙樹（不依賴 playwright，方便離線測試）
# ──────────────────────────────────────────────────────────────────────────
def flatten_tree(snapshot):
    """把無障礙樹壓成一維 token 串，每個 token 保留 role / name / url。"""
    tokens = []

    def walk(node):
        if not node:
            return
        role = node.get("role", "")
        name = (node.get("name", "") or "").strip()
        url = node.get("url", "") or ""
        children = node.get("children", [])
        if role in _TEXT_ROLES and name:
            tokens.append({"role": role, "name": name, "url": url})
            return
        if not children and name:
            tokens.append({"role": role, "name": name, "url": url})
            return
        for child in children:
            walk(child)

    walk(snapshot)
    return tokens


def extract_post_id(url):
    """從任意 Facebook 連結反查貼文 ID；找不到回 None。"""
    if not url:
        return None
    for pat in _POST_ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _is_post_header(tokens, i):
    """tokens[i] 是否為一篇貼文的作者標頭 (heading 後面緊跟貼文版面標記)。"""
    if tokens[i]["role"] != "heading":
        return False
    for j in range(i + 1, min(len(tokens), i + 6)):
        name = tokens[j]["name"]
        if _ACTION_RE.search(name) or _AUDIENCE_RE.match(name):
            return True
    return False


def _find_post_headers(tokens):
    """回傳所有貼文標頭的索引；主要用 heading 偵測，失敗則退回 ⋯ 動作按鈕。"""
    headers = [i for i in range(len(tokens)) if _is_post_header(tokens, i)]
    if headers:
        return headers
    # 後備方案：版面若改版導致 heading 偵測失效，改用每篇必有的「⋯動作按鈕」
    fallback = []
    for i, t in enumerate(tokens):
        if _ACTION_RE.search(t["name"]):
            # 作者標頭通常在動作按鈕前 1~3 個 token
            fallback.append(max(0, i - 3))
    return fallback


def _header_content_start(tokens, h, end):
    """回傳該貼文「內文」開始的索引（跳過作者列、時間戳記、分享對象、動作按鈕）。"""
    for j in range(h + 1, min(end, h + 8)):
        if _ACTION_RE.search(tokens[j]["name"]):
            return j + 1
        if _AUDIENCE_RE.match(tokens[j]["name"]):
            # 動作按鈕通常緊接在分享對象之後
            if j + 1 < end and _ACTION_RE.search(tokens[j + 1]["name"]):
                return j + 2
            return j + 1
    return h + 1


def _relative_time_to_dt(num, unit, now):
    num = int(num)
    if unit == "分鐘":
        return now - timedelta(minutes=num)
    if unit == "小時":
        return now - timedelta(hours=num)
    if unit == "天":
        return now - timedelta(days=num)
    if unit == "週":
        return now - timedelta(weeks=num)
    if unit == "個月":
        return now - timedelta(days=30 * num)
    if unit == "年":
        return now - timedelta(days=365 * num)
    return None


def _parse_visible_time(text, now):
    """把還原出的可見時間字串（相對或絕對）轉成 datetime；失敗回 None。"""
    if not text:
        return None
    t = re.sub(r"\s+", "", text)
    if "剛剛" in t or "剛才" in t:
        return now
    # 先試絕對日期（需同時有「月」「日」）：（YYYY年）M月D日（HH:MM）
    # 必須先於相對時間，否則「2025年5月26日」會被誤判成「2025 年前」
    m = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日(?:.*?(\d{1,2}):(\d{2}))?", t)
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        try:
            dt = datetime(year, int(m.group(2)), int(m.group(3)),
                          int(m.group(4)) if m.group(4) else 0,
                          int(m.group(5)) if m.group(5) else 0)
        except ValueError:
            return None
        # 省略年份且日期落在未來 → 視為去年
        if not m.group(1) and dt > now:
            dt = dt.replace(year=year - 1)
        return dt
    # 相對時間：X分鐘 / X分 / X小時 / X時 / X天 / X週 / X個月 / X年
    m = re.search(r"(\d+)\s*(分鐘|分|小時|時|天|週|個月|年)", t)
    if m:
        unit = {"分": "分鐘", "時": "小時"}.get(m.group(2), m.group(2))
        return _relative_time_to_dt(m.group(1), unit, now)
    return None


def extract_posts_from_snapshot(snapshot, group_token, keywords=KEYWORDS, now=None):
    """
    純擷取邏輯：吃一棵無障礙樹，吐出符合關鍵字的貼文清單。

    group_token: 社團識別字串（vanity 例如 'NTU.Head' 或數字 ID），用來組永久連結。
    """
    if now is None:
        now = datetime.now()
    tokens = flatten_tree(snapshot)
    headers = _find_post_headers(tokens)

    posts = []
    for k, h in enumerate(headers):
        end = headers[k + 1] if k + 1 < len(headers) else len(tokens)
        block = tokens[h:end]
        author = tokens[h]["name"]
        content_start = _header_content_start(tokens, h, end)

        # ── 網址：先找帶 ID 的連結組成標準網址，否則退回時間戳記混淆連結 ──
        post_id = None
        ts_fallback = None
        for t in block:
            if post_id is None:
                post_id = extract_post_id(t["url"])
            if (ts_fallback is None and t["url"] and "__cft__" in t["url"]
                    and extract_post_id(t["url"]) is None):
                ts_fallback = t["url"]
        if post_id:
            post_url = f"https://www.facebook.com/groups/{group_token}/posts/{post_id}/"
        else:
            post_url = ts_fallback or "未找到連結"

        # ── 發文時間（盡力而為）：只接受「時間戳記連結」區的乾淨相對時間。 ──
        # Facebook 會打亂貼文時間文字，所以多半抓不到 → None；但絕不誤抓內文
        # 裡的「倒數 4 天」之類字串。
        post_time = None
        for t in tokens[h:min(end, content_start + 2)]:
            if t["role"] != "link":
                continue
            m = _TIME_RE.search(re.sub(r"\s+", "", t["name"]))
            if m:
                post_time = _relative_time_to_dt(m.group(1), m.group(2), now)
                break

        # ── 內文：從內文起點收集，碰到互動列/留言區就停 ──
        content = []
        for t in tokens[content_start:end]:
            name = t["name"]
            if _ENGAGE_RE.search(name):
                break
            if _CHROME_RE.search(name) or _AUDIENCE_RE.match(name):
                continue
            content.append(name)
        full_text = "\n".join(content).strip()

        if not full_text:
            continue
        if keywords and not any(kw in full_text for kw in keywords):
            continue

        posts.append({
            "author": author,
            "post_id": post_id,
            "post_url": post_url,
            "post_time": post_time.strftime("%Y-%m-%d %H:%M:%S") if post_time else None,
            "scraped_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "text": full_text,
        })

    # ── 去重：有 ID 用 ID，否則用內文 ──
    deduped = []
    seen_ids = set()
    seen_text = set()
    for p in posts:
        if p["post_id"]:
            if p["post_id"] in seen_ids:
                continue
            seen_ids.add(p["post_id"])
        else:
            key = re.sub(r"\s+", "", p["text"])
            if key in seen_text:
                continue
            seen_text.add(key)
        deduped.append(p)

    return deduped


# ──────────────────────────────────────────────────────────────────────────
# 即時擷取（需 playwright + 已登入的 Chrome）
# ──────────────────────────────────────────────────────────────────────────
def _group_token_from_url(url):
    """從目前頁面網址抓出社團識別字（vanity 或數字 ID）。"""
    if url:
        m = re.search(r"/groups/([^/?#]+)", url)
        if m and m.group(1) not in ("feed",):
            return m.group(1)
    return None


def reconstruct_post_times(page):
    """以版面還原各貼文的可見時間，回傳 {post_id: 可見時間字串}。不移動滑鼠、不捲動。"""
    try:
        return page.evaluate(_TIME_RECON_JS) or {}
    except Exception as e:
        print(f"[警告] 時間還原失敗（沿用 None）: {e}")
        return {}


def extract_posts_via_a11y(page, dump_debug=True):
    print("[*] 正在解析無障礙樹 (貼文分段 + 貼文 ID 反查網址)...")

    group_token = _group_token_from_url(page.url) or "248305265374276"
    snapshot = get_a11y_tree_cdp(page)

    if dump_debug:
        try:
            with open("debug_a11y_tree_dump.json", "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[警告] 寫入除錯檔失敗: {e}")

    candidates = extract_posts_from_snapshot(snapshot, group_token)

    # 還原被混淆的發文時間，並用 post_id 對應回各貼文
    times = reconstruct_post_times(page)
    now = datetime.now()
    for c in candidates:
        vt = times.get(c["post_id"]) if c["post_id"] else None
        if vt:
            c["post_time_text"] = vt
            dt = _parse_visible_time(vt, now)
            if dt:
                c["post_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")

    found_ids = sum(1 for c in candidates if c["post_id"])
    found_time = sum(1 for c in candidates if c["post_time"])
    print("-" * 40)
    print("[DEBUG 報告]")
    print(f"社團識別字: {group_token}")
    print(f"符合關鍵字的貼文: {len(candidates)} 篇"
          f"（{found_ids} 篇取得標準永久連結、{found_time} 篇還原出發文時間）")
    print("-" * 40)
    return candidates


def save_html_periodically(min_sec=3000, max_sec=3900, rest_start=22, rest_end=5):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        page = browser.contexts[0].pages[0]

        print("開始監聽並提取網頁資料...")
        while True:
            now = datetime.now()

            # 休息時段（如需啟用請取消註解）
            # if is_rest_time(now, rest_start, rest_end):
            #     print(f"[{now.strftime('%H:%M:%S')}] 休息時段，暫停運作。")
            #     time.sleep(600)
            #     continue

            print(f"[{now.strftime('%H:%M:%S')}] 正在重新整理網頁以獲取最新貼文...")
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(5000)  # 轉圈完後保險多等 5 秒
            except Exception as e:
                print(f"[警告] 重新整理失敗: {e}")

            try:
                results = extract_posts_via_a11y(page)
            except Exception as e:
                print(f"[警告] 本次擷取失敗: {e}")
                results = []

            if not results:
                print("\n[警告] 本次抓取資料為 0 筆！")
                print("若關鍵字確實有命中卻抓到 0，代表 Facebook 可能改版，")
                print("請打開 debug_a11y_tree_dump.json 檢查貼文新結構。\n")

            for idx, item in enumerate(results):
                print(f"--- 潛在遺失物貼文 {idx + 1} ---")
                print(f"作者: {item['author']}")
                print(f"連結: {item['post_url']}")
                print(f"推算發文時間: {item['post_time'] or '未知(FB 混淆時間戳記)'}")
                print(f"內文預覽:\n{item['text'][:150]}...\n")

            if results:
                timestamp = now.strftime("%Y%m%d-%H%M%S")
                filename = f"NTU_Head_{timestamp}.json"
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=4)
                print(f"[*] 已將 {len(results)} 筆資料儲存至 {filename}")

            sleep_time = random.randint(min_sec, max_sec)
            print(f"等待 {sleep_time} 秒後進行下一次抓取...\n")
            time.sleep(sleep_time)


def _run_offline_test(dump_path, group_token):
    """離線驗證：直接吃已存的無障礙樹 dump，列出擷取結果。"""
    with open(dump_path, encoding="utf-8") as f:
        snapshot = json.load(f)
    results = extract_posts_from_snapshot(snapshot, group_token)
    print(f"[離線測試] 來源: {dump_path}  社團識別字: {group_token}")
    print(f"[離線測試] 命中關鍵字貼文: {len(results)} 篇\n")
    for i, r in enumerate(results):
        print(f"=== 貼文 {i + 1} ===")
        print(f"作者     : {r['author']}")
        print(f"貼文 ID  : {r['post_id']}")
        print(f"連結     : {r['post_url']}")
        print(f"發文時間 : {r['post_time'] or '未知'}")
        print(f"內文     : {r['text'][:200]}")
        print()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook 社團遺失物擷取器")
    parser.add_argument("--test", metavar="DUMP_JSON",
                        help="離線測試：用已存的無障礙樹 dump 驗證擷取邏輯")
    parser.add_argument("--group", default="NTU.Head",
                        help="社團識別字 (離線測試用)，預設 NTU.Head")
    args = parser.parse_args()

    if args.test:
        _run_offline_test(args.test, args.group)
    else:
        save_html_periodically()
