# 在 Cog 中使用 mod

## 標準引入

```python
from mods.logger import setup_logger
from mods.storage import get_storage

logger = setup_logger(__name__)
```

## 建議模式

```python
class MyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = get_storage()
```

## 規則摘要

- 使用 `get_storage()` 取得主程式建立的單例
- 使用 `setup_logger(__name__)` 取得 logger
- 不要在 cog 中呼叫 `init_storage()` / `close_storage()`
- 不要直接 `DataStore(...)` 自建連線