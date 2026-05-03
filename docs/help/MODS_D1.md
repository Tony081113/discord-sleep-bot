# mods/d1.ts

Cloudflare Workers 端使用的 D1 代理工具。

## 引入

```ts
import { D1Client, DiscordId, BatchStatement } from "./mods/d1";
const db = new D1Client(env.DB);
```

## 方法

| 方法 | 說明 |
|------|------|
| `query<T>(sql, params?)` | SELECT，回傳列資料 |
| `execute(sql, params?)` | 單一寫入語句 |
| `batch(statements)` | 批次執行多語句 |
| `raw(sql)` | 直接執行任意 SQL |

## Discord ID 規範

Discord 雪花 ID 在 JS/TS 必須用字串表示，並在 D1 使用 `TEXT` 欄位儲存。