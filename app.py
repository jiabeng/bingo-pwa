
# app.py — Render-hardened + GET/POST + JSON error handlers (Bingo Bingo helper)
# -*- coding: utf-8 -*-
import os, sys, json, csv, sqlite3, threading, time, re
from datetime import datetime, date, timedelta
from collections import Counter
from typing import List, Dict, Any, Optional

from flask import Flask, jsonify, render_template, send_from_directory, request, send_file
import requests
from bs4 import BeautifulSoup

# —— 在 Render 上略過不完整憑證鏈的驗證警告（台彩 API/官網 HTML）——
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---- 基本設定 ----
API_URL  = os.getenv("BINGO_API_URL", "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/LatestBingoResult")
DB_PATH  = os.getenv("DB_PATH",  os.path.join("data", "bingo.db"))
CSV_PATH = os.getenv("CSV_PATH", os.path.join("data", "bingo_super.csv"))
TOP_K    = int(os.getenv("TOP_K", "10"))
MIN_TODAY_ROWS_FOR_RECO = int(os.getenv("MIN_TODAY_ROWS_FOR_RECO", "15"))

os.makedirs("data", exist_ok=True)
app = Flask(__name__, static_folder="static", template_folder="templates")

# ---- CORS / Cache 控制 ----
@app.after_request
def add_common_headers(resp):
    # API 與 debug 路徑開放 CORS 並避免快取
    if request.path.startswith("/api") or request.path.startswith("/debug"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
    return resp

# ---- DB ----
SCHEMA_SQL = (
    "CREATE TABLE IF NOT EXISTS bingo_super ("
    " draw_term INTEGER PRIMARY KEY,"
    " draw_time TEXT NOT NULL,"
    " super_number INTEGER NOT NULL,"
    " open_order TEXT NOT NULL,"
    " high_low TEXT,"
    " odd_even TEXT,"
    " fetched_at TEXT NOT NULL)"
)

def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(SCHEMA_SQL)
    return conn
CONN = db_conn()

# ---- 小工具 ----
def append_csv(row: Dict[str, Any]):
    exists = os.path.isfile(CSV_PATH)
    with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=[
            "draw_term","draw_time","super_number","open_order","high_low","odd_even","fetched_at"
        ])
        if not exists:
            w.writeheader()
        w.writerow({
            "draw_term": row["draw_term"],
            "draw_time": row["draw_time"],
            "super_number": row["super_number"],
            "open_order": ",".join(row["open_order"]) if isinstance(row["open_order"], list) else row["open_order"],
            "high_low": row.get("high_low"),
            "odd_even": row.get("odd_even"),
            "fetched_at": row["fetched_at"],
        })

def upsert_row(row: Dict[str, Any]):
    cur = CONN.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO bingo_super(draw_term, draw_time, super_number, open_order, high_low, odd_even, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["draw_term"], row["draw_time"], row["super_number"],
            json.dumps(row["open_order"], ensure_ascii=False) if isinstance(row["open_order"], list) else row["open_order"],
            row.get("high_low"), row.get("odd_even"), row["fetched_at"]
        )
    )
    CONN.commit()

# ---- 安全請求（具備重試 + 退避） ----
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def safe_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15, max_retries: int = 5):
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    delay = 1.0
    for attempt in range(max_retries):
        try:
            res = requests.get(url, headers=h, timeout=timeout, verify=False, allow_redirects=True)
            txt = res.text or ""
            blocked = any(k in txt for k in ["cf-browser-verification", "Access denied", "Attention Required"]) or res.status_code in (403, 503)
            if blocked:
                raise RuntimeError(f"blocked or challenged (status={res.status_code})")
            return res
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay + (0.2 * attempt))
            delay = min(delay * 2, 15)

# ---- 擷取官網最新一期（官方 API） ----

def fetch_latest() -> Dict[str, Any]:
    r = requests.get(API_URL, timeout=10, verify=False, headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    post = data.get("content", {}).get("lotteryBingoLatestPost") or {}
    open_order = post.get("openShowOrder")
    if isinstance(open_order, str):
        parts = re.findall(r"\d{1,2}", open_order)
        open_order = [p.zfill(2) for p in parts]
    elif isinstance(open_order, list):
        open_order = [str(x).zfill(2) for x in open_order]
    else:
        open_order = []
    super_n = post.get("prizeNum", {}).get("bullEye")
    try:
        super_n = int(super_n)
    except Exception:
        super_n = int(open_order[-1]) if open_order else -1
    return {
        "draw_term": int(post.get("drawTerm", 0)),
        "draw_time": str(post.get("dDate", datetime.now().isoformat(timespec='seconds'))),
        "open_order": open_order,
        "super_number": super_n,
        "high_low": post.get("prizeNum", {}).get("highLow"),
        "odd_even": post.get("prizeNum", {}).get("oddEven"),
        "fetched_at": datetime.now().isoformat(timespec='seconds')
    }

# ---- 每逢整 5 分鐘（07:00、07:05、…）的睡眠計算 ----

def seconds_until_next_five_minute() -> int:
    now = datetime.now()
    current_block = (now.minute // 5) * 5
    next_block = current_block + 5
    if next_block >= 60:
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_block, second=0, microsecond=0)
    return int((next_time - now).total_seconds())

# ---- 背景：每整 5 分鐘抓「最新一期」 ----

def polling_loop():
    while True:
        try:
            latest = fetch_latest()
            if latest.get("draw_term"):
                upsert_row(latest)
                append_csv(latest)
        except Exception as e:
            print("[WARN] fetch failure:", e, file=sys.stderr)
        time.sleep(max(5, seconds_until_next_five_minute()))

threading.Thread(target=polling_loop, daemon=True).start()

# ---- HTML 解析：抓取今天所有已開獎期數（官網公開頁面）----

def parse_today_from_official_html(debug_save: bool = True) -> List[Dict[str, Any]]:
    candidate_urls = [
        "https://www.taiwanlottery.com.tw/lottery/Lotto/BingoBingo",
        "https://www.taiwanlottery.com.tw/lottery/Lotto/BingoBingo/index.html",
        "https://www.taiwanlottery.com/lottery/Lotto/BingoBingo",
        "https://www.taiwanlottery.com/lottery/Lotto/BingoBingo/index.html",
    ]
    ua_headers = {"Referer": "https://www.taiwanlottery.com.tw/"}

    html = None
    used_url = None
    last_error: Optional[str] = None

    for url in candidate_urls:
        try:
            res = safe_get(url, headers=ua_headers, timeout=15, max_retries=4)
            txt = res.text or ""
            if res.status_code == 200 and len(txt) > 1500 and ("賓果賓果" in txt or "Bingo" in txt or "BINGO" in txt):
                html = txt
                used_url = url
                break
            else:
                last_error = f"bad content len={len(txt)} status={res.status_code}"
        except Exception as e:
            last_error = str(e)
            continue

    if html:
        try:
            os.makedirs("data", exist_ok=True)
            with open(os.path.join("data", "last_today.html"), "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print("[WARN] save last_today.html fail:", e, file=sys.stderr)
    else:
        print("[WARN] official html not available; last_error=", last_error, file=sys.stderr)
        return []

    soup = BeautifulSoup(html, "html.parser")

    term_re = re.compile(r"(?:第)?(\d{8,12})\s*期")
    nums_re = re.compile(r"(?:(?:^|\D)(\d{1,2})(?!\d)(?:(?:\s|,|、|，|．|・|:|；|/|\-))+){19}(\d{1,2})(?!\d)")

    def z2(n: str) -> str:
        return str(int(n)).zfill(2)

    rows: List[Dict[str, Any]] = []
    seen = set()

    containers = []
    for sel in ['[id*="today"]','[class*="today"]','[id*="bingo"]','[class*="bingo"]','main','section','article','table','div']:
        containers.extend(soup.select(sel))
    containers = [c for c in containers if c.get_text(strip=True) and len(c.get_text()) > 500]

    for cont in containers:
        text = cont.get_text(" ", strip=True)
        for m in term_re.finditer(text):
            term = int(m.group(1))
            if term in seen:
                continue
            start = max(0, m.start() - 480)
            end   = min(len(text), m.end() + 480)
            window = text[start:end]
            mnum = nums_re.search(window)
            if not mnum:
                continue
            raw = re.findall(r"\d{1,2}", mnum.group(0))
            if len(raw) != 20:
                continue
            open_order = [z2(x) for x in raw]
            super_n = int(open_order[-1])
            draw_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            rows.append({
                "draw_term": term,
                "draw_time": draw_time,
                "open_order": open_order,
                "super_number": super_n,
                "high_low": None,
                "odd_even": None,
            })
            seen.add(term)

    if not rows:
        full_text = soup.get_text(" ", strip=True)
        for m in term_re.finditer(full_text):
            term = int(m.group(1))
            if term in seen:
                continue
            start = max(0, m.start() - 640)
            end   = min(len(full_text), m.end() + 640)
            window = full_text[start:end]
            mnum = nums_re.search(window)
            if not mnum:
                continue
            raw = re.findall(r"\d{1,2}", mnum.group(0))
            if len(raw) != 20:
                continue
            open_order = [z2(x) for x in raw]
            super_n = int(open_order[-1])
            draw_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            rows.append({
                "draw_term": term,
                "draw_time": draw_time,
                "open_order": open_order,
                "super_number": super_n,
                "high_low": None,
                "odd_even": None,
            })
            seen.add(term)

    if not rows:
        scripts = soup.find_all("script")
        arr_pattern = re.compile(r"\[(?:\s*"?\d{1,2}"?\s*,){19}\s*"?\d{1,2}"?\s*\]")
        for sc in scripts:
            txt = sc.string or sc.get_text() or ""
            if not txt or len(txt) < 200:
                continue
            for arr in arr_pattern.findall(txt):
                nums = re.findall(r"\d{1,2}", arr)
                if len(nums) != 20:
                    continue
                pos = txt.find(arr)
                start = max(0, pos - 1200)
                end   = min(len(txt), pos + 1200)
                win   = txt[start:end]
                mterm = term_re.search(win)
                if not mterm:
                    mm = list(term_re.finditer(txt))
                    if mm:
                        mterm = mm[-1]
                if not mterm:
                    continue
                term = int(mterm.group(1))
                if term in seen:
                    continue
                open_order = [z2(x) for x in nums]
                super_n = int(open_order[-1])
                draw_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                rows.append({
                    "draw_term": term,
                    "draw_time": draw_time,
                    "open_order": open_order,
                    "super_number": super_n,
                    "high_low": None,
                    "odd_even": None,
                })
                seen.add(term)

    rows = sorted({r["draw_term"]: r for r in rows}.values(), key=lambda x: x["draw_term"])
    print(f"[BACKFILL SOURCE] url={used_url} parsed={len(rows)}", file=sys.stderr)
    return rows

# ---- 資料庫批次寫入 ----

def upsert_many(rows: List[Dict[str, Any]]) -> int:
    cur = CONN.cursor()
    inserted = 0
    for r in rows:
        try:
            cur.execute(
                "INSERT OR IGNORE INTO bingo_super(draw_term, draw_time, super_number, open_order, high_low, odd_even, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r["draw_term"],
                    r["draw_time"],
                    r["super_number"],
                    json.dumps(r["open_order"], ensure_ascii=False),
                    r.get("high_low"),
                    r.get("odd_even"),
                    datetime.now().isoformat(timespec='seconds')
                )
            )
            inserted += cur.rowcount
        except Exception as e:
            print("[WARN] insert fail:", e, file=sys.stderr)
    CONN.commit()
    return inserted


def backfill_today_once() -> Dict[str, Any]:
    rows = parse_today_from_official_html()
    if not rows:
        return {"ok": False, "inserted": 0, "parsed": 0}
    inserted = upsert_many(rows)
    return {"ok": True, "inserted": inserted, "parsed": len(rows)}

# ---- 背景：每 30 分鐘自動補齊一次今天資料 ----

def backfill_scheduler_loop():
    while True:
        try:
            info = backfill_today_once()
            print("[BACKFILL]", info, file=sys.stderr)
        except Exception as e:
            print("[BACKFILL ERR]", e, file=sys.stderr)
        time.sleep(30 * 60)

threading.Thread(target=backfill_scheduler_loop, daemon=True).start()

# ---- 當日統計 + 推薦 ----

def parse_dt(dt_str: str) -> datetime:
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return datetime.strptime(dt_str.replace('Z',''), "%Y-%m-%dT%H:%M:%S")


def query_today_rows() -> List[Dict[str, Any]]:
    today = date.today()
    cur = CONN.cursor()
    cur.execute("SELECT draw_term, draw_time, super_number, open_order FROM bingo_super ORDER BY draw_term ASC")
    out: List[Dict[str, Any]] = []
    for term, dtime, sn, ojson in cur.fetchall():
        dt = parse_dt(dtime)
        if dt.date() == today:
            out.append({
                "draw_term": term,
                "draw_time": dt.isoformat(),
                "super_number": int(sn),
                "open_order": json.loads(ojson) if isinstance(ojson, str) else ojson,
            })
    return out


def recency_unique(seq: List[int], take: int = 20) -> List[int]:
    last = list(seq)[-take:]
    seen, ordered = set(), []
    for n in reversed(last):
        if n not in seen:
            ordered.append(n); seen.add(n)
    return ordered


def recommend_numbers(today_supers: List[int], freq_top: List[Any]) -> Dict[str, Any]:
    if len(today_supers) >= MIN_TODAY_ROWS_FOR_RECO and freq_top:
        base = [n for (n, _) in freq_top]
        return {"pick1": base[:1], "pick3": base[:3] if len(base) >= 3 else base,
                "pick5": base[:5] if len(base) >= 5 else base,
                "rationale": "使用『今日熱度排行』做等配分散。"}
    cur = CONN.cursor()
    cur.execute("SELECT super_number FROM bingo_super ORDER BY draw_term ASC")
    all_supers = [int(r[0]) for r in cur.fetchall()]
    today_top = [n for (n, _) in Counter(today_supers).most_common(2)]
    rec_seq = recency_unique(all_supers[-50:], take=20)
    pool: List[int] = []
    for n in today_top + rec_seq[:8]:
        if n not in pool: pool.append(n)
    while len(pool) < 5 and rec_seq:
        x = rec_seq.pop(0)
        if x not in pool: pool.append(x)
    return {"pick1": pool[:1], "pick3": pool[:3], "pick5": pool[:5],
            "rationale": "今日樣本不足：混合『今日熱度』+『近20期輪替前段』。"}

# ---- Routes ----

@app.get("/")
def home():
    try:
        return render_template("index.html")
    except Exception:
        return "<h3>Bingo PWA API</h3><p>Use /api/* endpoints.</p>", 200, {"Content-Type": "text/html; charset=utf-8"}

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

@app.get("/api/latest")
def latest():
    cur = CONN.cursor()
    cur.execute("SELECT draw_term, draw_time, super_number, open_order, high_low, odd_even, fetched_at FROM bingo_super ORDER BY draw_term DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "message": "no data"})
    term, dtime, super_n, ojson, hl, oe, ft = row
    return jsonify({
        "ok": True,
        "draw_term": int(term),
        "draw_time": dtime,
        "super_number": int(super_n),
        "open_order": json.loads(ojson) if isinstance(ojson, str) else ojson,
        "high_low": hl,
        "odd_even": oe,
        "fetched_at": ft
    })

# 🔘 立即更新（只抓最新一期）
@app.post("/api/force-update")
def force_update():
    try:
        latest = fetch_latest()
        if latest.get("draw_term"):
            upsert_row(latest)
            append_csv(latest)
        return jsonify({"ok": True, "latest": latest})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# 📅 一鍵補齊今天所有資料（解析官網 HTML）— 同時支援 GET 與 POST
@app.route("/api/fetch-today-full", methods=["GET", "POST"])
def api_fetch_today_full():
    info = backfill_today_once()
    return jsonify(info)

@app.get("/api/today-count")
def api_today_count():
    rows = query_today_rows()
    return jsonify({"ok": True, "today_count": len(rows)})

@app.get("/api/today")
def today_api():
    rows = query_today_rows()
    supers = [r["super_number"] for r in rows]
    freq_top = Counter(supers).most_common(TOP_K) if supers else []
    rec_u = recency_unique(supers, take=20) if supers else []
    reco = recommend_numbers(supers, freq_top) if supers else {"pick1":[], "pick3":[], "pick5":[], "rationale":"尚無資料"}
    return jsonify({
        "ok": True,
        "today_count": len(rows),
        "latest": rows[-1] if rows else None,
        "freq_top": [{"number":int(n), "count":int(c)} for (n,c) in freq_top],
        "last20": supers[-20:],
        "recency_unique": rec_u,
        "recommend": reco
    })

@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")

@app.get("/sw.js")
def sw():
    return send_from_directory("static", "sw.js", mimetype="text/javascript")

# ====== Debug endpoints（避免 SW/快取造成 Loading）======
@app.get("/debug/last-html-head")
def debug_last_html_head():
    path = os.path.join("data", "last_today.html")
    if not os.path.isfile(path):
        return "No last_today.html yet (請先呼叫 /api/fetch-today-full)", 404
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        head = f.read(2000)
    return f"<pre style='white-space:pre-wrap;font-family:monospace'>{head}</pre>", 200, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
    }

@app.get("/debug/last-html-download")
def debug_last_html_download():
    path = os.path.join("data", "last_today.html")
    if not os.path.isfile(path):
        return "No last_today.html yet (請先呼叫 /api/fetch-today-full)", 404
    return send_file(path, as_attachment=True, download_name="last_today.html", mimetype="text/html")

@app.get("/debug/last-html")
def debug_last_html():
    path = os.path.join("data", "last_today.html")
    if not os.path.isfile(path):
        return "No last_today.html yet (請先呼叫 /api/fetch-today-full)", 404
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}

# ---- API 統一錯誤處理（確保回 JSON，而不是 HTML）----
from werkzeug.exceptions import HTTPException

@app.errorhandler(404)
def err_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not_found", "path": request.path}), 404
    return e, 404

@app.errorhandler(405)
def err_405(e):
    if request.path.startswith("/api/"):
        return jsonify({
            "ok": False,
            "error": "method_not_allowed",
            "path": request.path,
            "allowed": list(getattr(e, "valid_methods", []) or []),
        }), 405
    return e, 405

@app.errorhandler(Exception)
def err_500(e):
    if request.path.startswith("/api/"):
        code = 500
        if isinstance(e, HTTPException):
            code = e.code or 500
        return jsonify({"ok": False, "error": str(e)}), code
    raise e

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
