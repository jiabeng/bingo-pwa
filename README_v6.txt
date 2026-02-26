
# Bingo Bingo Data API — v6（Option A）

**最新一期 → 官方 JSON；補齊今天 → 暫走 pilio**

## 啟動
- 本機：`python app.py`
- Gunicorn：`gunicorn app:app --preload --timeout 120 --workers 2`

## 端點
- `GET /api/latest`  
  從 `https://api.taiwanlottery.com/TLCAPIWeB/Lottery/LatestBingoResult` 取得最新一期（含 20 顆 + 超級獎號）。
- `GET /api/fetch-today-full?source=pilio`  
  暫時以 pilio 列表頁解析當日**全部**期別，回傳陣列；`source=official` 保留，待官方多期端點接回。
- `GET /debug/official-snapshot`  
  抓官網結果頁（SPA）並寫檔 `last_today.html`。
- `GET /debug/last-html-head`、`GET /debug/last-html-download`  
  檢視/下載 `last_today.html`。

> 注意：開獎資訊以台灣彩券官網公布為準。本服務僅供資料彙整與學術研究。
