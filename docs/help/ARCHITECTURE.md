# 架構與生命週期

## 架構概覽

```text
main.py
│
├── init_mods()          開啟 Redis / D1 連接池、掛載 Redis 日誌 Handler
├── load_cogs()          載入 cogs/ 內所有 cog
│
cogs/
└── *.py                 只呼叫 get_storage() / setup_logger()，不持有連線
```

## 連接池生命週期規則

| 動作 | 負責方 | 函數 |
|------|--------|------|
| 建立 Redis / D1 連接池 | `main.py` | `init_storage()` |
| 啟動 Redis → D1 背景同步 | `DataStore.connect()` | 自動 |
| 掛載 Redis 日誌 Handler | `main.py` | `attach_redis()` |
| 釋放所有連線 | `main.py` | `close_storage()` |
| Cog 取得儲存實例 | 各 cog | `get_storage()` |
| Cog 取得 logger | 各 cog | `setup_logger(__name__)` |

## 重要限制

- Cog 不應呼叫 `init_storage()`
- Cog 不應呼叫 `close_storage()`
- `main.py` 是連接池生命週期的唯一擁有者