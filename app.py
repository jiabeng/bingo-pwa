#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, json, csv, sqlite3, threading, time
from datetime import datetime, date, timedelta
from collections import Counter
from flask import Flask, jsonify, render_template, send_from_directory, request
import requests
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
    cur = CONN.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO bingo_super(draw_term, draw_time, super_number, open_order, high_low, odd_even, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["draw_term"], row["draw_time"], row["super_number"],
            json.dumps(row["open_order"], ensure_ascii=False),
            row["high_low"], row["odd_even"], row["fetched_at"]
        )
    )
    CONN.commit()

# ---- 擷取官網最新一期 ----
def fetch_latest():
    # 關鍵：verify=False 以略過台彩 API 在 Render 上的嚴格憑證驗證問題
    r = requests.get(API_URL, timeout=10, verify=False)
    r.raise_for_status()
    data = r.json()

    # 依照台彩官方回傳結構取值（lotteryBingoLatestPost）
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

# ---- 背景擷取：改為「每逢整 5 分鐘」 ----
def seconds_until_next_five_minute():
    now = datetime.now()
    current_block = (now.minute // 5) * 5
    next_block = current_block + 5
    if next_block >= 60:
        next_time = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(minute=next_block, second=0, microsecond=0)
    return (next_time - now).total_seconds()

def polling_loop():
    while True:
        try:
            latest = fetch_latest()
            upsert_row(latest)
            append_csv(latest)
        except Exception as e:
            print("[WARN] fetch failure:", e, file=sys.stderr)
        # 對齊到下一個整 5 分鐘
        time.sleep(max(1, int(seconds_until_next_five_minute())))

threading.Thread(target=polling_loop, daemon=True).start()

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

# 🔘 立即更新（前端一按就強制抓一次）
@app.post("/api/force-update")
def force_update():
    try:
        latest = fetch_latest()
        upsert_row(latest)
        append_csv(latest)
        return jsonify({"ok": True, "latest": latest})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/today")
def today():
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
