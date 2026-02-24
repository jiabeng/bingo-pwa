#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, csv, sqlite3, threading, time, re
from datetime import datetime, date, timedelta
from collections import Counter
from flask import Flask, jsonify, render_template, send_from_directory, request
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
app = Flask(__name__)

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

def append_csv(row: dict):
    exists = os.path.isfile(CSV_PATH)
    with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=[
            "draw_term","draw_time","super_number","open_order","high_low","odd_even","fetched_at"
        ])
        if not exists: w.writeheader()
        w.writerow({
            "draw_term": row["draw_term"],
            "draw_time": row["draw_time"],
            "super_number": row["super_number"],
            "open_order": ",".join(row["open_order"]),
            "high_low": row["high_low"],
            "odd_even": row["odd_even"],
            "fetched_at": row["fetched_at"],
        })

def upsert_row(row: dict):
    """單筆：同一期就覆寫（確保最新資料），不同期會新增。"""
    cur = CONN.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO bingo_super(draw_term, draw_time, super_number, open_order, high_low, odd_even, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["draw_term"], row["draw_time"], row["super_number"],
            json.dumps(row["open_order"], ensure_ascii=False),
            row.get("high_low"), row.get("odd_even"), row["fetched_at"]
        )
    )
    CONN.commit()

# ---- 擷取官網最新一期 ----
def fetch_latest():
    # Render 對台彩 API 憑證鏈嚴格：加 verify=False 以通過
    r = requests.get(API_URL, timeout=10, verify=False)
    r.raise_for_status()
    data = r.json()
    post = data["content"]["lotteryBingoLatestPost"]
    return {
        "draw_term": int(post["drawTerm"]),
        "draw_time": post["dDate"],
        "open_order": post["openShowOrder"],
        "super_number": int(post["prizeNum"]["bullEye"]),
        "high_low": post["prizeNum"].get("highLow"),
        "odd_even": post["prizeNum"].get("oddEven"),
        "fetched_at": datetime.now().isoformat(timespec='seconds')
    }

# ---- 每逢整 5 分鐘（07:00、07:05、…）的睡眠計算 ----
def seconds_until_next_five_minute():
    now = datetime.now()
    current_block = (now.minute // 5) * 5
    next_block = current_block + 5
    if next_block >= 60:
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_block, second=0, microsecond=0)
    return (next_time - now).total_seconds()

# ---- 背景：每整 5 分鐘抓「最新一期」 ----
def polling_loop():
    while True:
        try:
            latest = fetch_latest()
            upsert_row(latest)
            append_csv(latest)
        except Exception as e:
            print("[WARN] fetch failure:", e, file=sys.stderr)
        # 對齊到下一個整 5 分鐘
        sleep_s = max(1, int(seconds_until_next_five_minute()))
        time.sleep(sleep_s)

threading.Thread(target=polling_loop, daemon=True).start()

# ---- HTML 解析：抓取今天所有已開獎期數（官網公開頁面）----
def parse_today_from_official_html():
    """
    從台彩 Bingo Bingo 官方今日頁面抓取『今天所有已開獎期別』。
    回傳 list[dict]（與 DB 欄位對齊）：
      { draw_term:int, draw_time:strISO(只保日), open_order:list[str], super_number:int, high_low, odd_even }
    """
    candidate_urls = [
        "https://www.taiwanlottery.com/lottery/Lotto/BingoBingo",
    ]
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"}

    html = None
    for url in candidate_urls:
        try:
            res = requests.get(url, headers=ua, timeout=12, verify=False)
            if res.status_code == 200 and len(res.text) > 1500:
                html = res.text
                break
        except Exception as e:
            print("[WARN] fetch official html failed:", e, file=sys.stderr)

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    # 嘗試找大型容器（保守做法），再用 regex 抽取期別與 20 顆數字
    containers = []
    for sel in ['[id*="today"]','[class*="today"]','[id*="bingo"]','[class*="bingo"]','section','div']:
        containers.extend(soup.select(sel))
    containers = [c for c in containers if c.get_text(strip=True) and len(c.get_text()) > 500]

    term_re = re.compile(r"(?:第)?(\d{8,12})\s*期")
    nums_re = re.compile(r"(?:(?:^|\\D)(\\d{1,2})(?!\\d)(?:(?:\\s|,|、|，))+){19}(\\d{1,2})(?!\\d)")

    def z2(n: str) -> str:
        return str(int(n)).zfill(2)

    seen_terms = set()
    rows = []

    for cont in containers:
        text = cont.get_text(" ", strip=True)
        for m in term_re.finditer(text):
            term = int(m.group(1))
            if term in seen_terms:
                continue
            start = max(0, m.start() - 240)
            end   = min(len(text), m.end() + 240)
            window = text[start:end]
            mnum = nums_re.search(window)
            if not mnum:
                continue
            raw = re.findall(r"\\d{1,2}", mnum.group(0))
            if len(raw) != 20:
                continue
            open_order = [z2(x) for x in raw]
            super_n = int(open_order[-1])
            # draw_time：保存今天日期（具體時間以 API 為準）
            draw_time = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            row = {
                "draw_term": term,
                "draw_time": draw_time,
                "open_order": open_order,
                "super_number": super_n,
                "high_low": None,
                "odd_even": None,
            }
            rows.append(row)
            seen_terms.add(term)

    rows = sorted({r["draw_term"]: r for r in rows}.values(), key=lambda x: x["draw_term"])
    return rows

def upsert_many(rows: list[dict]) -> int:
    """批次寫入今天多期，使用 INSERT OR IGNORE 確保不重覆。"""
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
            inserted += cur.rowcount  # 1 or 0
        except Exception as e:
            print("[WARN] insert fail:", e, file=sys.stderr)
    CONN.commit()
    return inserted

def backfill_today_once() -> dict:
    """抓取官方 HTML → 解析 → 寫入 DB，回傳插入筆數與解析數"""
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
        time.sleep(30 * 60)  # 30 分鐘後再試

threading.Thread(target=backfill_scheduler_loop, daemon=True).start()

# ---- 當日統計 + 推薦 ----
def parse_dt(dt_str: str) -> datetime:
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return datetime.strptime(dt_str.replace('Z',''), "%Y-%m-%dT%H:%M:%S")

def query_today_rows():
    today = date.today()
    cur = CONN.cursor()
    cur.execute("SELECT draw_term, draw_time, super_number, open_order FROM bingo_super ORDER BY draw_term ASC")
    out = []
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

def recency_unique(seq, take=20):
    last = list(seq)[-take:]
    seen, ordered = set(), []
    for n in reversed(last):    # 新→舊 去重
        if n not in seen:
            ordered.append(n); seen.add(n)
    return ordered

def recommend_numbers(today_supers, freq_top):
    if len(today_supers) >= MIN_TODAY_ROWS_FOR_RECO and freq_top:
        base = [n for (n, _) in freq_top]
        return {
            "pick1": base[:1],
            "pick3": base[:3] if len(base)>=3 else base,
            "pick5": base[:5] if len(base)>=5 else base,
            "rationale": "使用『今日熱度排行』做等配分散。"
        }
    # 今日樣本不足 → 混合 今日Top + 近20期輪替前段
    cur = CONN.cursor()
    cur.execute("SELECT super_number FROM bingo_super ORDER BY draw_term ASC")
    all_supers = [r[0] for r in cur.fetchall()]
    today_top = [n for (n, _) in Counter(today_supers).most_common(2)]
    rec_seq = recency_unique(all_supers[-50:], take=20)
    pool = []
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

# ---- Routes ----
@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

@app.get("/api/latest")
def latest():
    cur = CONN.cursor()
    cur.execute("SELECT draw_term, draw_time, super_number, open_order, high_low, odd_even, fetched_at "
                "FROM bingo_super ORDER BY draw_term DESC LIMIT 1")
    row = cur.fetchone()
    if not row: return jsonify({"ok": False, "message": "no data"})
    term, dtime, super_n, ojson, hl, oe, ft = row
    return jsonify({
        "ok": True,
        "draw_term": term,
        "draw_time": dtime,
        "super_number": super_n,
        "open_order": json.loads(ojson),
        "high_low": hl,
        "odd_even": oe,
        "fetched_at": ft
    })

# 🔘 立即更新（只抓最新一期）
@app.post("/api/force-update")
def force_update():
    try:
        latest = fetch_latest()
        upsert_row(latest)
        append_csv(latest)
        return jsonify({"ok": True, "latest": latest})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# 📅 一鍵補齊今天所有資料（解析官網 HTML）
@app.post("/api/fetch-today-full")
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
        "freq_top": [{"number":n, "count":c} for (n,c) in freq_top],
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

if __name__ == "__main__":
    # 在 Render 上請把 Start Command 設為：python app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
