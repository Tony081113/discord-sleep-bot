# Discord Sleep Bot — 開發者指南

本文件說明專案中所有 **mod**（功能模組）的用途、可調用函數清單、引入方式，以及連接池的生命週期規則。

---

## 目錄

1. [架構概覽](#架構概覽)
2. [連接池生命週期規則](#連接池生命週期規則)
3. [mod 清單](#mod-清單)
   - [mods/logger.py — 標準化日誌](#modsloggerpy--標準化日誌)
   - [mods/storage.py — 統一儲存層 (Redis + D1)](#modsstoragepy--統一儲存層-redis--d1)
   - [mods/d1.ts — TypeScript D1 代理](#modsd1ts--typescript-d1-代理)
4. [在 Cog 中引入 mod](#在-cog-中引入-mod)
5. [環境變數參考](#環境變數參考)

---

## 架構概覽

```
main.py
│
├── init_mods()          ← 開啟 Redis / D1 連接池、啟動日誌 Redis Handler
├── load_cogs()          ← 動態載入 cogs/ 目錄下所有 cog
│
cogs/
└── your_cog.py          ← 只呼叫 get_storage() / setup_logger()，不持有連線
```

- **`main.py`** 負責所有 mod 的初始化與釋放，是連接池的**唯一擁有者**。
- **Cog** 透過 `get_storage()` 取得已初始化的 `DataStore` 單例，**不應自行呼叫 `init_storage()` 或 `close_storage()`**。

---

## 連接池生命週期規則

| 動作 | 負責方 | 函數 |
|------|--------|------|
| 建立 Redis / D1 連接池 | `main.py` | `init_storage()` |
| 啟動 Redis → D1 背景同步 | 自動（`DataStore.connect()` 內） | — |
| 掛載 Redis 日誌 Handler | `main.py` | `attach_redis()` |
| 釋放所有連線 | `main.py` | `close_storage()` |
| **Cog 取得儲存實例** | **各 cog** | **`get_storage()`** |
| **Cog 取得 logger** | **各 cog** | **`setup_logger(__name__)`** |

> **重要**：Cog 中**永遠不要**呼叫 `init_storage()` 或 `close_storage()`。
> 連接池由 `main.py` 統一管理，各 cog 只持有 `DataStore` 的參照。

---

## mod 清單

### `mods/logger.py` — 標準化日誌

提供統一的結構化日誌，同時輸出至：
- **Console**（stdout）
- **本地輪轉檔案**（每天切換，保留 30 天，預設目錄 `logs/`）
- **Redis 列表**（key: `bot:logs`，每筆 JSON 記錄，上限 100,000 條）

#### 引入方式

```python
from mods.logger import setup_logger, attach_redis, get_configured_logger_names
```

#### 函數清單

| 函數 | 說明 | 回傳值 |
|------|------|--------|
| `setup_logger(name, *, log_dir, log_level, redis_client)` | 建立或取得具名 logger。通常在模組頂層呼叫一次 `setup_logger(__name__)`。若同名已建立則回傳快取實例，不重複建立。 | `logging.Logger` |
| `attach_redis(logger_name, redis_client)` | 為已建立的 logger 掛上 Redis handler。重複呼叫安全（不重複掛載）。 | `None` |
| `get_configured_logger_names()` | 回傳所有透過 `setup_logger` 建立的 logger 名稱清單。 | `list[str]` |

#### 參數說明

**`setup_logger`**

| 參數 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `name` | `str` | — | Logger 名稱，通常傳入 `__name__` |
| `log_dir` | `str \| None` | `LOG_DIR` 環境變數或 `"logs"` | 日誌檔案目錄 |
| `log_level` | `str \| None` | `LOG_LEVEL` 環境變數或 `"INFO"` | 最低日誌等級（`DEBUG`/`INFO`/`WARNING`/`ERROR`） |
| `redis_client` | `redis.Redis \| None` | `None` | 已連線的**同步** Redis 客戶端，提供後自動掛載 Redis handler |

#### 使用範例

```python
# cogs/sleep.py
from mods.logger import setup_logger

logger = setup_logger(__name__)

class SleepCog(commands.Cog):
    async def some_command(self, ctx):
        logger.info("User %s used sleep command", ctx.author.id)
        logger.error("Something went wrong", exc_info=True)
```

---

### `mods/storage.py` — 統一儲存層 (Redis + D1)

提供 Redis（熱快取）+ Cloudflare D1（持久化 SQL）雙後端儲存。
任一後端不可用時系統可降級運作。

#### 引入方式

```python
# Cog 中只需引入以下三個（schema 輔助類按需引入）：
from mods.storage import get_storage
from mods.storage import ColumnDef, ForeignKey, TableSchema  # 按需引入
```

`main.py` 負責引入並呼叫生命週期函數：

```python
# main.py（已配置，不需在 cog 中重複）
from mods.storage import init_storage, close_storage
```

#### 模組級函數

| 函數 | 說明 |
|------|------|
| `get_storage() → DataStore` | 取得已初始化的單例。若尚未初始化則拋出 `RuntimeError`。 |
| `await init_storage(**kwargs) → DataStore` | 建立並連接 `DataStore`（由 `main.py` 呼叫）。 |
| `await close_storage()` | 釋放所有連線與背景任務（由 `main.py` 呼叫）。 |

#### `DataStore` 公開方法

##### 生命週期（main.py 呼叫，cog 無需使用）

| 方法 | 說明 |
|------|------|
| `await connect()` | 連接 Redis 與 D1，啟動背景同步任務。 |
| `await close()` | 取消同步任務，關閉所有連線。 |
| `get_sync_redis_client()` | 回傳同步 `redis.Redis` 客戶端（供日誌 handler 使用）。 |

##### 快取 API（Cache-Aside，Redis → D1 降級）

| 方法 | 說明 | 回傳值 |
|------|------|--------|
| `await get(key)` | 讀取快取值。先查 Redis，未命中再查 D1，並回寫 Redis。 | `str \| None` |
| `await set(key, value, ttl=None, *, persist=True)` | 寫入 Redis（可設 TTL），`persist=True` 時同步寫入 D1。 | `None` |
| `await delete(key)` | 從 Redis 與 D1 同時刪除。 | `None` |
| `await exists(key)` | 檢查 key 是否存在於 Redis 或 D1。 | `bool` |

`set()` 的參數說明：

| 參數 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `key` | `str` | — | 儲存鍵名 |
| `value` | `str \| Any` | — | 字串或可 JSON 序列化的值 |
| `ttl` | `int \| None` | `None` | Redis TTL（秒）；`None` 表示永不過期 |
| `persist` | `bool` | `True` | 是否立即同步寫入 D1 |

> **提示**：以 `sync:` 為前綴的 Redis key（例如 `sync:user:42`）會由背景任務定期同步至 D1 `redis_cache` 表。

##### Redis 專用輔助方法

| 方法 | 說明 | 回傳值 |
|------|------|--------|
| `await redis_keys(pattern="*")` | 回傳符合 pattern 的 key 清單（全掃描，大資料量請改用 `scan_iter`）。 | `list[str]` |
| `await hset(name, mapping)` | 批次設置 Redis hash 欄位。 | `None` |
| `await hgetall(name)` | 取得 Redis hash 的所有欄位與值。 | `dict` |
| `await lpush(key, *values)` | 將值推入 Redis list 頭部。 | `int`（列表長度） |
| `await pipeline()` | 取得 Redis pipeline，用於批次指令。 | `redis.Pipeline` |

##### D1 SQL API

| 方法 | 說明 | 回傳值 |
|------|------|--------|
| `await execute(sql, params=None)` | 執行 SQL（含寫入），回傳所有結果列。 | `list[dict]` |
| `await fetchall(sql, params=None)` | `execute` 的語意別名（適合 SELECT）。 | `list[dict]` |
| `await fetchone(sql, params=None)` | 回傳第一列，若無結果回傳 `None`。 | `dict \| None` |
| `await enable_foreign_keys()` | 啟用 SQLite/D1 外鍵約束（預設關閉）。 | `None` |
| `await define_table(schema)` | 根據 `TableSchema` 建立資料表（若不存在）。 | `None` |

SQL 參數使用 `?` 佔位符：

```python
store = get_storage()
rows = await store.fetchall(
    "SELECT * FROM sleep_records WHERE user_id = ?",
    [str(ctx.author.id)],
)
```

##### Schema 輔助類（可選）

用於宣告式建表，避免手寫 DDL：

```python
from mods.storage import ColumnDef, ForeignKey, TableSchema

store = get_storage()
await store.enable_foreign_keys()

await store.define_table(TableSchema(
    name="guilds",
    columns=[
        ColumnDef("guild_id", "TEXT", primary_key=True),
        ColumnDef("name",     "TEXT", not_null=True),
    ],
))

await store.define_table(TableSchema(
    name="sleep_records",
    columns=[
        ColumnDef("id",         "INTEGER", primary_key=True, autoincrement=True),
        ColumnDef("user_id",    "TEXT",    not_null=True),   # Discord ID → TEXT
        ColumnDef("guild_id",   "TEXT",    not_null=True),   # Discord ID → TEXT
        ColumnDef("channel_id", "TEXT",    not_null=True),   # Discord ID → TEXT
        ColumnDef("slept_at",   "TEXT",    not_null=True),
        ColumnDef("woke_at",    "TEXT"),
    ],
    foreign_keys=[
        ForeignKey("guild_id", "guilds", "guild_id", on_delete="CASCADE"),
    ],
))
```

**`ColumnDef` 參數**

| 參數 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `name` | `str` | — | 欄位名稱 |
| `type` | `str` | — | SQLite 型別：`TEXT`、`INTEGER`、`REAL`、`BLOB`、`NUMERIC` |
| `primary_key` | `bool` | `False` | 設為主鍵 |
| `autoincrement` | `bool` | `False` | 自動遞增（僅 `INTEGER PRIMARY KEY` 有效） |
| `not_null` | `bool` | `False` | 加上 `NOT NULL` 約束 |
| `unique` | `bool` | `False` | 加上 `UNIQUE` 約束 |
| `default` | `str \| None` | `None` | 原始 SQL 預設值運算式，例如 `"0"` 或 `"datetime('now')"` |

**`ForeignKey` 參數**

| 參數 | 類型 | 預設值 | 說明 |
|------|------|--------|------|
| `column` | `str` | — | 本表欄位名稱 |
| `ref_table` | `str` | — | 被參照的父表 |
| `ref_column` | `str` | — | 父表中被參照的欄位 |
| `on_delete` | `str` | `"NO ACTION"` | 刪除父列時的行為：`CASCADE`、`SET NULL`、`RESTRICT` 等 |
| `on_update` | `str` | `"NO ACTION"` | 更新父列時的行為（同上） |

#### 可用屬性

| 屬性 | 類型 | 說明 |
|------|------|------|
| `redis_available` | `bool` | Redis 連線是否可用 |
| `d1_available` | `bool` | D1 連線是否可用 |

---

### `mods/d1.ts` — TypeScript D1 代理

適用於執行於 **Cloudflare Workers** 的 TypeScript/JavaScript 端。
提供對 Cloudflare D1 的零邏輯、無狀態代理，四個公開方法對應四種操作類型。

#### 引入方式

```ts
import { D1Client, DiscordId, BatchStatement } from "./mods/d1";

// env.DB 為 wrangler.toml 中定義的 D1Database binding
const db = new D1Client(env.DB);
```

#### 方法清單

| 方法 | 說明 | 回傳值 |
|------|------|--------|
| `query<T>(sql, params?)` | 執行 SELECT，回傳所有結果列。 | `Promise<T[]>` |
| `execute(sql, params?)` | 執行單一寫入語句（INSERT / UPDATE / DELETE）。 | `Promise<ExecResult>` |
| `batch(statements)` | 一次 round-trip 執行多個語句。 | `Promise<BatchResult[]>` |
| `raw(sql)` | 直接執行任意 SQL（PRAGMA、DDL、多語句腳本）。 | `Promise<RawResult>` |

#### 公開型別

| 型別 | 說明 |
|------|------|
| `DiscordId = string` | Discord 雪花 ID 必須儲存為 TEXT，在 TypeScript 中型別為 `string`。 |
| `ExecResult` | `{ changes, last_row_id, duration }` — `execute` 的回傳值。 |
| `BatchStatement` | `{ sql, params? }` — `batch` 的輸入項目。 |
| `BatchResult<T>` | `{ rows, changes, duration }` — `batch` 每個語句的回傳值。 |
| `RawResult` | `{ count, duration }` — `raw` 的回傳值。 |

#### Discord ID 儲存規範

> Discord 雪花 ID 超出 JavaScript 安全整數範圍，**必須以 TEXT 儲存**。

| Discord 概念 | D1 欄位型別 | TypeScript 型別 |
|-------------|------------|-----------------|
| Guild ID    | `TEXT`     | `DiscordId`     |
| Channel ID  | `TEXT`     | `DiscordId`     |
| Role ID     | `TEXT`     | `DiscordId`     |
| User ID     | `TEXT`     | `DiscordId`     |

#### 使用範例

```ts
// 建立資料表（含外鍵）
await db.raw("PRAGMA foreign_keys = ON;");
await db.raw(`
  CREATE TABLE IF NOT EXISTS guilds (
    guild_id TEXT PRIMARY KEY,
    name     TEXT NOT NULL
  )
`);

// 寫入
await db.execute(
  "INSERT INTO guilds (guild_id, name) VALUES (?, ?)",
  ["123456789012345678", "My Server"]
);

// 讀取（型別安全）
const rows = await db.query<{ guild_id: DiscordId; name: string }>(
  "SELECT * FROM guilds WHERE guild_id = ?",
  ["123456789012345678"]
);

// 批次寫入
await db.batch([
  { sql: "INSERT INTO guilds (guild_id, name) VALUES (?, ?)", params: [id1, "A"] },
  { sql: "INSERT INTO guilds (guild_id, name) VALUES (?, ?)", params: [id2, "B"] },
]);
```

---

## 在 Cog 中引入 mod

Cog 只需引入以下兩行，**不持有連線、不管理生命週期**：

```python
# cogs/sleep.py
from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)   # 在模組頂層宣告一次

class SleepCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # 取得 main.py 已初始化的 DataStore 單例
        self.store = get_storage()

    @commands.command()
    async def sleep(self, ctx: commands.Context) -> None:
        user_key = f"user:{ctx.author.id}:sleeping"
        await self.store.set(user_key, "true", ttl=28800)  # 8 小時 TTL
        logger.info("User %s went to sleep", ctx.author.id)
        await ctx.send("晚安！")

    @commands.command()
    async def wake(self, ctx: commands.Context) -> None:
        rows = await self.store.fetchall(
            "SELECT * FROM sleep_records WHERE user_id = ? ORDER BY slept_at DESC LIMIT 1",
            [str(ctx.author.id)],
        )
        if rows:
            await ctx.send(f"你上次睡眠記錄：{rows[0]}")
        else:
            await ctx.send("沒有找到睡眠記錄。")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SleepCog(bot))
```

### Cog 規則摘要

| 規則 | 說明 |
|------|------|
| ✅ 使用 `get_storage()` | 取得 main.py 建立的單例，輕量且安全 |
| ✅ 使用 `setup_logger(__name__)` | 取得或建立具名 logger |
| ❌ 不呼叫 `init_storage()` | 會建立第二個連接池 |
| ❌ 不呼叫 `close_storage()` | 會提前關閉其他 cog 共用的連線 |
| ❌ 不直接建立 `DataStore(...)` | 繞過生命週期管理 |

---

## 環境變數參考

複製 `.env.example` 並填入值：

```bash
cp .env.example .env
```

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DISCORD_TOKEN` | — | Discord Bot Token（必填） |
| `CLOUDFLARE_ACCOUNT_ID` | — | Cloudflare 帳戶 ID |
| `CLOUDFLARE_D1_DATABASE_ID` | — | D1 資料庫 ID |
| `CLOUDFLARE_API_TOKEN` | — | Cloudflare API Token |
| `REDIS_HOST` | `localhost` | Redis 主機 |
| `REDIS_PORT` | `6379` | Redis 連接埠 |
| `REDIS_PASSWORD` | （空） | Redis 密碼（無密碼留空） |
| `REDIS_DB` | `0` | Redis 資料庫編號 |
| `LOG_LEVEL` | `INFO` | 日誌等級（`DEBUG`/`INFO`/`WARNING`/`ERROR`） |
| `LOG_DIR` | `logs` | 本地日誌目錄 |
| `REDIS_SYNC_INTERVAL` | `60` | Redis → D1 背景同步間隔（秒） |
