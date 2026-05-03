# Discord Sleep Bot

Discord 伺服器防護與復原機器人，提供「異常偵測 + 核准式復原 + Web 管理面板」。

## 核心功能

- 異常偵測：監控頻道/身分組刪除、修改，以及管理員權限移除
- 復原流程：核准者按鈕觸發，還原頻道、角色與近期訊息
- Web 面板：Discord OAuth2 登入、復原請求審核、手動復原、門檻設定、維護日誌
- 儲存架構：Redis（快取）+ Cloudflare D1（持久化）
- 系統指令：`/ping`、`/status`、`/panel`、`/accept-approver`、`/remove-approver`

## 快速開始

### 1. 安裝依賴

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 建立環境變數

```bash
copy .env.example .env
```

至少需設定：

- `DISCORD_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_D1_DATABASE_ID`
- `CLOUDFLARE_API_TOKEN`

若要啟用 Web 面板，另外設定：

- `WEB_PORT`
- `WEB_BASE_URL`
- `WEB_SECRET`（建議 32 字元以上）
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`

### 3. 啟動 Bot

```bash
python main.py
```

啟動後可用 `/panel` 取得面板網址。

## 面板預覽（Puppeteer）

若要在不登入 Discord OAuth 的情況下直接查看 Web 面板樣貌，可使用本機預覽模式與 Puppeteer 截圖。

### 1. 安裝預覽依賴

```bash
npm install
```

### 2. 產生預覽截圖

```bash
npm run panel:preview
```

預設會產出以下頁面的截圖到 `.panel-preview/`：

- `overview`
- `recovery`
- `thresholds`
- `developer`

若只想看單一頁面：

```bash
npm run panel:preview -- --page recovery
```

這個模式會開啟 `/?preview=1&page=...`，前端會注入 mock API 資料，所以不需要啟動 bot 或完成 Discord 登入。

## 專案結構

```text
main.py
cogs/
mods/
web/
docs/
```

## 文件導覽

- 文件總覽: [docs/README.md](docs/README.md)
- 開發者入口（相容舊連結）: [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)

## 疑難排解

- 若 Discord 按鈕顯示「此交互失敗」，先查看 [docs/help/TROUBLESHOOTING.md](docs/help/TROUBLESHOOTING.md)
- 若 OAuth2 顯示 redirect_uri 無效，請檢查 Discord Developer Portal 的 redirect URI 與 `WEB_BASE_URL`

## License

目前未附授權條款，若要開源建議補上 LICENSE。