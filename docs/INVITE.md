# 邀請機器人加入伺服器

## 邀請連結格式

將下方 URL 中的 `YOUR_CLIENT_ID` 替換為 `.env` 中的 `DISCORD_CLIENT_ID` 值：

```
https://discord.com/api/oauth2/authorize?client_id=1494557452497195049&permissions=8&scope=bot%20applications.commands
```

`permissions=8` 代表 **Administrator**，機器人需要管理員權限才能執行完整的復原流程（建立/編輯頻道、身分組、封鎖成員、修改伺服器外觀）。

---

## 需要哪些 OAuth2 Scope？

| Scope                  | 用途                        |
|------------------------|-----------------------------|
| `bot`                  | 讓機器人帳號加入伺服器      |
| `applications.commands`| 啟用 Slash 指令（`/defense`、`/status` 等） |

---

## 需要哪些 Bot 權限？

| 權限               | 原因                             |
|--------------------|----------------------------------|
| Administrator      | 包含以下所有項目（建議選此項）   |
| *(或個別選擇)*     |                                  |
| Manage Guild       | 還原伺服器名稱、圖示、橫幅       |
| Manage Channels    | 還原被刪除/修改的頻道            |
| Manage Roles       | 還原被刪除/修改的身分組          |
| Ban Members        | 封鎖訊息轟炸者                   |
| Read Message History | 加密保存訊息                   |
| Send Messages      | 發送系統回覆                     |
| Embed Links        | 傳送警報卡片（Embed）            |

---

## 快速步驟

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications)，選取你的應用程式。
2. 左側點選 **OAuth2 → URL Generator**。
3. Scopes 勾選 `bot` 與 `applications.commands`。
4. Bot Permissions 勾選 **Administrator**（或逐項勾選上表）。
5. 複製產生的 URL，在瀏覽器開啟即可邀請機器人。

---

## 邀請後的必要設定

邀請成功後，請在伺服器執行以下 Slash 指令完成初始化：

```
/accept-approver        設定可執行復原操作的核准者
/panel                  取得 Web 管理面板網址
```

詳細設定請參考 [help/COG_USAGE.md](help/COG_USAGE.md)。
