# R2 Guardian Daemon

資料流：`DB-mod -> Guardian Daemon -> Cloudflare R2`

## 1) 設定 daemon 專屬環境變數

```powershell
Copy-Item daemon/.env.example daemon/.env
```

填好 `daemon/.env` 後，啟動守護程式：

```powershell
python daemon/app.py
```

## 2) Bot 端設定（DB-mod 連 daemon）

在專案根目錄 `.env` 新增：

- `DBMOD_DAEMON_ENABLED=1`
- `DBMOD_DAEMON_INIT_DB_ON_START=1`
- `DBMOD_DAEMON_URL=http://127.0.0.1:8091`
- `DBMOD_DAEMON_TOKEN=<要和 daemon 的 DAEMON_EXPECTED_DBMOD_TOKEN 一致>`
- `DBMOD_EXPECT_DAEMON_TOKEN=<要和 daemon 的 DAEMON_SELF_TOKEN 一致>`

Bot 啟動時會做：

1. handshake（token 互認）
2. 可選 init-db（部署前置）

## API

- `GET /health`：存活檢查（無需 auth）
- `POST /v1/handshake`：驗證 DB-mod token
- `POST /v1/init-db`：初始化 daemon 本地 metadata DB
- `POST /v1/r2/put`：寫入 R2 物件
