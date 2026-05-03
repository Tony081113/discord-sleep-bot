# mods/storage.py

統一儲存層：Redis（快取）+ Cloudflare D1（持久化 SQL）。

## 模組級函數

| 函數 | 說明 |
|------|------|
| `get_storage()` | 取得已初始化 `DataStore` 單例 |
| `await init_storage(**kwargs)` | 建立並連線 `DataStore`（由 `main.py` 呼叫） |
| `await close_storage()` | 關閉連線與背景同步（由 `main.py` 呼叫） |

## DataStore 主要方法

### 快取 API

- `await get(key)`
- `await set(key, value, ttl=None, persist=True)`
- `await delete(key)`
- `await exists(key)`

### SQL API

- `await execute(sql, params=None)`
- `await fetchall(sql, params=None)`
- `await fetchone(sql, params=None)`
- `await enable_foreign_keys()`
- `await define_table(schema)`

## 屬性

- `redis_available`
- `d1_available`

## 注意事項

- SQL 參數使用 `?` 佔位符
- Discord Snowflake ID 請以 `TEXT` 儲存