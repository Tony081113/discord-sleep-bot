# mods/logger.py

提供統一的結構化日誌輸出到：

- Console
- 本地輪轉檔案（`logs/`）
- Redis list（`bot:logs`）

## 引入

```python
from mods.logger import setup_logger, attach_redis, get_configured_logger_names
```

## 函數

| 函數 | 說明 | 回傳值 |
|------|------|--------|
| `setup_logger(name, *, log_dir, log_level, redis_client)` | 建立或取得具名 logger | `logging.Logger` |
| `attach_redis(logger_name, redis_client)` | 為 logger 掛上 Redis handler（重複呼叫安全） | `None` |
| `get_configured_logger_names()` | 回傳目前已配置的 logger 名稱 | `list[str]` |

## 典型使用

```python
from mods.logger import setup_logger

logger = setup_logger(__name__)
logger.info("service started")
logger.error("unexpected error", exc_info=True)
```