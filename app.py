
# app.py — v4: more robust backfill + always save HTML + pilio fallback
# -*- coding: utf-8 -*-
import os, sys, json, csv, sqlite3, threading, time, re
from datetime import datetime, date, timedelta
from collections import Counter
from typing import List, Dict, Any, Optional

from flask import Flask, jsonify, render_template, send_from_directory, request, send_file
import requests
from bs4 import BeautifulSoup

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API_URL  = os.getenv("BINGO_API_URL", "https://api.taiwanlottery.com/TLCAPIWeB/Lottery/LatestBingoResult")
DB_PATH  = os.getenv("DB_PATH",  os.path.join("data", "bingo.db"))
CSV_PATH = os.getenv("CSV_PATH", os.path.join("data", "bingo_super.csv"))
TOP_K    = int(os.getenv("TOP_K", "10"))
MIN_TODAY_ROWS_FOR_RECO = int(os.getenv("MIN_TODAY_ROWS_FOR_RECO", "15"))

os.makedirs("data", exist_ok=True)
app = Flask(__name__, static_folder="static", template_folder="templates")

@app.after_request
def add_common_headers(resp):
    if request.path.startswith("/api") or request.path.startswith("/debug"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "no-store"
    return resp

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

# ---------- helpers ----------

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
    last_exc = None
    for attempt in range(max_retries):
        try:
            res = requests.get(url, headers=h, timeout=timeout, verify=False, allow_redirects=True)
            return res
        except Exception as e:
            last_exc = e
            if attempt == max_retries - 1:
                raise
            time.sleep(delay + (0.2 * attempt))
            delay = min(delay * 2, 15)
    if last_exc:
        raise last_exc

# ---------- fetch latest ----------

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

# ---------- scheduler for latest ----------

def seconds_until_next_five_minute() -> int:
    now = datetime.now()
    current_block = (now.minute // 5) * 5
    next_block = current_block + 5
    if next_block >= 60:
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_block, second=0, microsecond=0)
    return int((next_time - now).total_seconds())


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

# ---------- parsers ----------

def parse_today_from_official_html(debug_save: bool = True) -> List[Dict[str, Any]]:
    candidate_urls = [
        # 官方結果頁（可能是伺服器端渲染/或部分嵌入資料）
        "https://www.taiwanlottery.com/lotto/result/bingo_bingo/?searchData=true",
        # 新舊路徑
        "https://www.taiwanlottery.com.tw/lottery/Lotto/BingoBingo",
        "https://www.taiwanlottery.com.tw/lottery/Lotto/BingoBingo/index.html",
        "https://www.taiwanlottery.com/lottery/Lotto/BingoBingo",
        "https://www.taiwanlottery.com/lottery/Lotto/BingoBingo/index.html",
    ]
    ua_headers = {"Referer": "https://www.taiwanlottery.com.tw/"}

    html: Optional[str] = None
    used_url: Optional[str] = None
    last_snapshot: Optional[str] = None

    for url in candidate_urls:
        try:
            res = safe_get(url, headers=ua_headers, timeout=15, max_retries=3)
            txt = res.text or ""
            # 只要長度夠，我們就保存並嘗試解析（不再強制關鍵字），以避免你看到 "No last_today.html yet"
            if len(txt) > 500:
                html = txt
                used_url = url
                break
            else:
                last_snapshot = f"status={res.status_code} len={len(txt)}"
        except Exception as e:
            last_snapshot = f"error: {e}"
            continue

    # 一律保存最近一次成功取回的 HTML（即便解析失敗）
    if html and debug_save:
        try:
            os.makedirs("data", exist_ok=True)
            with open(os.path.join("data", "last_today.html"), "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print("[WARN] save last_today.html fail:", e, file=sys.stderr)
    else:
        print("[WARN] official html not available; last=", last_snapshot, file=sys.stderr)
        return []

    soup = BeautifulSoup(html, "html.parser")

    term_re = re.compile(r"(?:第)?(\d{8,12})\s*期")
    nums_re = re.compile(r"(?:(?:^|\D)(\d{1,2})(?!\d)(?:(?:\s|,|、|，|．|・|:|；|/|\-))+){19}(\d{1,2})(?!\d)")

    def z2(n: str) -> str:
        return str(int(n)).zfill(2)

    rows: List[Dict[str, Any]] = []
    seen = set()

    # 容器鄰近抽取
    containers = []
    for sel in ['[id*="today"]','[class*="today"]','[id*="bingo"]','[class*="bingo"]','main','section','article','table','div']:
        containers.extend(soup.select(sel))
    containers = [c for c in containers if c.get_text(strip=True) and len(c.get_text()) > 400]

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

    # 全頁回掃
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

    # script JSON 陣列
    if not rows:
        scripts = soup.find_all("script")
        arr_pattern = re.compile(r"\[(?:\s*\"?\d{1,2}\"?\s*,){19}\s*\"?\d{1,2}\"?\s*\]")
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
                    # fallback: 直接用鄰近最靠後的期別樣式
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

# ---- 第三方備援（僅用於官方頁面解析不到時；資料以官方為準） ----

def parse_today_from_pilio() -> List[Dict[str, Any]]:
    url = "https://www.pilio.idv.tw/bingo/list.asp"
    try:
        res = safe_get(url, headers={"Referer": "https://www.pilio.idv.tw/"}, timeout=15, max_retries=2)
    except Exception as e:
        print("[WARN] pilio fetch error:", e, file=sys.stderr)
        return []
    txt = res.text or ""
    if len(txt) < 500:
        return []
    # 保存第三方頁面，方便對照
    try:
        with open(os.path.join("data", "last_today_pilio.html"), "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception as e:
        print("[WARN] save pilio html fail:", e, file=sys.stderr)

    # 解析樣式：
    # 【期別: 115011453】 01, 06, ... 77  超級獎號:64 _ 猜大小:－ _ 猜單雙:－ (14:10)
    term_pat = re.compile(r"期別[:：]\s*(\d{8,12})")
    line_pat = re.compile(r"期別[:：]\s*(\d{8,12}).*?(?:\n|\r)")

    rows: List[Dict[str, Any]] = []
    for m in term_pat.finditer(txt):
        term = int(m.group(1))
        # 抓該期附近的 20 顆號碼
        start = max(0, m.start())
        end = min(len(txt), m.end() + 400)
        window = txt[start:end]
        nums = re.findall(r"\d{1,2}", window)
        # 期別後面會先出現20顆，再出現超級獎號；先抓 20 顆
        if len(nums) >= 21:
            open_order = [str(int(n)).zfill(2) for n in nums[:20]]
            super_n = int(nums[20])
            draw_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            rows.append({
                "draw_term": term,
                "draw_time": draw_time,
                "open_order": open_order,
                "super_number": super_n,
                "high_low": None,
                "odd_even": None,
            })
    rows = sorted({r["draw_term"]: r for r in rows}.values(), key=lambda x: x["draw_term"])
    print(f"[BACKFILL SOURCE PILIO] parsed={len(rows)}", file=sys.stderr)
    return rows

# ---------- write-many ----------

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

# ---------- backfill orchestrator ----------

def backfill_today_once() -> Dict[str, Any]:
    rows = parse_today_from_official_html()
    if not rows:
        # 官方解析不到 → 用第三方備援補今天
        rows = parse_today_from_pilio()
    if not rows:
        return {"ok": False, "inserted": 0, "parsed": 0}
    inserted = upsert_many(rows)
    return {"ok": True, "inserted": inserted, "parsed": len(rows)}

# ---------- scheduler for backfill ----------

def backfill_scheduler_loop():
    while True:
        try:
            info = backfill_today_once()
            print("[BACKFILL]", info, file=sys.stderr)
        except Exception as e:
            print("[BACKFILL ERR]", e, file=sys.stderr)
        time.sleep(30 * 60)

threading.Thread(target=backfill_scheduler_loop, daemon=True).start()

# ---------- today / recommend ----------

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
        return {
            "pick1": base[:1],
            "pick3": base[:3] if len(base) >= 3 else base,
            "pick5": base[:5] if len(base) >= 5 else base,
            "rationale": "使用『今日熱度排行』做等配分散。"
        }
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
    return {
        "pick1": pool[:1],
        "pick3": pool[:3],
        "pick5": pool[:5],
        "rationale": "今日樣本不足：混合『今日熱度』+『近20期輪替前段』。"
    }

# ---------- routes ----------

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

@app.route("/api/force-update", methods=["GET", "POST"])

def force_update():
    try:
        latest = fetch_latest()
        if latest.get("draw_term"):
            upsert_row(latest)
            append_csv(latest)
        return jsonify({"ok": True, "latest": latest})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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
        "freq_top": [{"number": int(n), "count": int(c)} for (n,c) in freq_top],
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

# ---- Debug ----
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

# JSON errors for API
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
