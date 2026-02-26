
# -*- coding: utf-8 -*-
"""
app.py v6 — Bingo Bingo 抓取服務（Option A）
-------------------------------------------------
- 最新一期：改打官網 JSON `LatestBingoResult`
- 補齊今天：暫時走備援 pilio 列表頁（可切換 source 參數）
- 保留 Debug：官方結果頁快照（SPA）寫檔、下載/預覽

啟動（本機測試）：
  python app.py

啟動（Gunicorn，建議）：
  gunicorn app:app --preload --timeout 120 --workers 2

注意：本服務僅供資料彙整與學習研究，開獎資訊以台灣彩券官網公布為準。
"""
import os
import re
import json
import time
import logging
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify, send_file, Response
from bs4 import BeautifulSoup

# -----------------------------
# 基本設定
# -----------------------------
BASE_URL = "https://api.taiwanlottery.com/TLCAPIWeB"  # 來自官網 Nuxt config 的 baseURL
OFFICIAL_SPA_URL = "https://www.taiwanlottery.com/lotto/result/bingo_bingo/?searchData=true"
PILIO_LIST_URL = "https://www.pilio.idv.tw/bingo/list.asp"

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
LAST_HTML = Path("last_today.html")
LAST_HTML_PILIO = DATA_DIR / "last_today_pilio.html"

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# 日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bingo_api")

# -----------------------------
# 共用：不快取 Header
# -----------------------------
@app.after_request
def add_no_cache_headers(resp: Response):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def json_error(message: str, status: int = 400, **extra):
    payload = {"ok": False, "error": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status

# -----------------------------
# 健康檢查 / 根路由
# -----------------------------
@app.route("/")
def root():
    return jsonify({
        "name": "Bingo Bingo Data API",
        "version": "v6",
        "endpoints": {
            "/api/latest": "最新一期（官方 JSON）",
            "/api/fetch-today-full?source=pilio": "補齊今天（暫走 pilio）",
            "/debug/official-snapshot": "抓取官網結果頁（SPA）並寫檔 last_today.html",
            "/debug/last-html-download": "下載 last_today.html",
            "/debug/last-html-head": "預覽 last_today.html 前 2000 字"
        }
    })

# -----------------------------
# 最新一期 → 官方 JSON `LatestBingoResult`
# -----------------------------
@app.route("/api/latest", methods=["GET"]) 
def api_latest():
    try:
        url = f"{BASE_URL}/Lottery/LatestBingoResult"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        j = r.json()
        data = j.get("content", {}).get("lotteryBingoLatestPost")
        if not data:
            return json_error("官方 API 回傳格式異常：缺少 content.lotteryBingoLatestPost", 502, raw=j)
        # 解析欄位
        period = str(data.get("drawTerm"))
        draw_time = data.get("dDate")  # ISO 時間字串
        open_show = data.get("openShowOrder") or []  # 開獎順序 20 顆（字串）
        big_show = data.get("bigShowOrder") or []    # 小到大 20 顆（字串）
        prize = (data.get("prizeNum") or {}).get("bullEye")
        try:
            numbers_open = [int(x) for x in open_show]
            numbers_sorted = [int(x) for x in big_show]
            super_ball = int(prize) if prize is not None else None
        except Exception:
            numbers_open = open_show
            numbers_sorted = big_show
            super_ball = prize
        return jsonify({
            "ok": True,
            "source": "official:LatestBingoResult",
            "period": period,
            "draw_time": draw_time,
            "numbers": numbers_open,
            "numbers_sorted": numbers_sorted,
            "super": super_ball
        })
    except requests.RequestException as e:
        logger.exception("/api/latest failed")
        return json_error("無法連線官方 API", 504, detail=str(e))
    except Exception as e:
        logger.exception("/api/latest error")
        return json_error("解析官方 API 失敗", 500, detail=str(e))

# -----------------------------
# 補齊今天（暫時走 pilio）
#   /api/fetch-today-full?source=pilio | official
#   official：預留（未實作多期列表 API 前）
# -----------------------------
PILIO_PATTERN = re.compile(r"【期別:\s*(\d{9,})】\s*([0-9,\s]+?)\s*超級獎號:(\d{1,2})")

@app.route("/api/fetch-today-full", methods=["GET"]) 
def api_fetch_today_full():
    source = (request.args.get("source") or "pilio").lower()
    if source not in ("pilio", "official"):
        return json_error("source 僅支援 pilio 或 official", 400)

    if source == "official":
        # 預留：等找到官網「當日多期列表」端點後回填
        return json_error("官方多期列表端點尚未接入，請先使用 source=pilio", 501)

    # 走 pilio 列表頁
    try:
        r = requests.get(PILIO_LIST_URL, timeout=20)
        r.raise_for_status()
        html = r.text
        try:
            LAST_HTML_PILIO.write_text(html, encoding="utf-8")
        except Exception:
            pass

        # 直接以純文字解析，避免依賴版面節點變動
        text = BeautifulSoup(html, "html.parser").get_text("
", strip=True)
        results = []
        for m in PILIO_PATTERN.finditer(text):
            period = m.group(1)
            nums_raw = m.group(2)
            nums = [int(x) for x in re.findall(r"\d{1,2}", nums_raw)][:20]
            super_ball = int(m.group(3))
            if len(nums) == 20:
                results.append({
                    "period": period,
                    "numbers": nums,
                    "super": super_ball
                })
        # 去重 + 依期別排序（由新到舊）
        unique = {}
        for row in results:
            unique[row["period"]] = row
        results = sorted(unique.values(), key=lambda x: x["period"], reverse=True)
        return jsonify({
            "ok": True,
            "source": "pilio",
            "count": len(results),
            "results": results
        })
    except requests.RequestException as e:
        logger.exception("/api/fetch-today-full failed (pilio)")
        return json_error("無法連線備援來源（pilio）", 504, detail=str(e))
    except Exception as e:
        logger.exception("/api/fetch-today-full error (pilio)")
        return json_error("解析備援來源（pilio）失敗", 500, detail=str(e))

# -----------------------------
# Debug：抓官網結果頁（SPA）寫檔 / 查看 / 下載
# -----------------------------
@app.route("/debug/official-snapshot", methods=["GET"]) 
def debug_official_snapshot():
    snapshots = []
    ok = True

    # (1) 抓官方結果頁（SPA）
    try:
        st = time.time()
        resp = requests.get(OFFICIAL_SPA_URL, timeout=20)
        took = int((time.time() - st) * 1000)
        length = len(resp.text)
        status = resp.status_code
        meta = {"url": OFFICIAL_SPA_URL, "status": status, "length": length, "took_ms": took}
        if status == 200 and length > 500:
            try:
                LAST_HTML.write_text(resp.text, encoding="utf-8")
                meta["saved"] = str(LAST_HTML)
            except Exception as e:
                meta["save_error"] = str(e)
        snapshots.append(meta)
    except Exception as e:
        ok = False
        snapshots.append({"url": OFFICIAL_SPA_URL, "error": str(e)})

    return jsonify({"ok": ok, "snapshots": snapshots})


@app.route("/debug/last-html-head", methods=["GET"]) 
def debug_last_html_head():
    if not LAST_HTML.exists():
        return json_error("last_today.html 不存在，請先呼叫 /debug/official-snapshot", 404)
    head = LAST_HTML.read_text(encoding="utf-8", errors="ignore")[:2000]
    return Response(head, mimetype="text/plain; charset=utf-8")


@app.route("/debug/last-html-download", methods=["GET"]) 
def debug_last_html_download():
    if not LAST_HTML.exists():
        return json_error("last_today.html 不存在，請先呼叫 /debug/official-snapshot", 404)
    return send_file(str(LAST_HTML), as_attachment=True, download_name="last_today.html")

# -----------------------------
# main
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
