# Bingo Bingo PWA（Render 版）

這是一個可安裝到手機主畫面的 PWA（Progressive Web App）。
- 後端：Flask（每 2 分鐘抓一次台彩官網最新一期 Bingo 資料）
- 前端：PWA（manifest + service worker），自動讀取 `/api/today` 顯示今日熱度/近20期/推薦

## 本機開發
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
打開 http://localhost:5000/ 測試。iPhone 請用 Safari 加到主畫面；Android 用 Chrome 安裝。

## Render 部署
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Instance Type: Free

部署成功後，用手機打開你的 HTTPS 網址即可安裝 PWA。
