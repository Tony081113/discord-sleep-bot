# 環境變數

## 基本設定

```bash
copy .env.example .env
```

## Discord / 儲存

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DISCORD_TOKEN` | — | Discord Bot Token（必填） |
| `CLOUDFLARE_ACCOUNT_ID` | — | Cloudflare 帳戶 ID |
| `CLOUDFLARE_D1_DATABASE_ID` | — | D1 資料庫 ID |
| `CLOUDFLARE_API_TOKEN` | — | Cloudflare API Token |
| `REDIS_HOST` | `localhost` | Redis 主機 |
| `REDIS_PORT` | `6379` | Redis 連接埠 |
| `REDIS_PASSWORD` | 空 | Redis 密碼 |
| `REDIS_DB` | `0` | Redis DB 編號 |
| `REDIS_SYNC_INTERVAL` | `60` | Redis → D1 同步間隔（秒） |

## 日誌

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `LOG_LEVEL` | `INFO` | 日誌等級 |
| `LOG_DIR` | `logs` | 本地日誌目錄 |

## Web 面板 / OAuth

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `WEB_HOST` | `0.0.0.0` | Web 綁定 host |
| `WEB_PORT` | `8080` | Web 連接埠 |
| `WEB_BASE_URL` | `http://localhost:{WEB_PORT}` | 對外網址（OAuth redirect 用） |
| `WEB_SECRET` | 隨機生成 | Session 簽名密鑰（建議 32 字元以上） |
| `DISCORD_CLIENT_ID` | — | OAuth2 Client ID |
| `DISCORD_CLIENT_SECRET` | — | OAuth2 Client Secret |