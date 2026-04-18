# 故障排查

## 常見問題

### 1. 按鈕顯示「此交互失敗」

- 先確認 bot 端是否有對應 `on_interaction` log
- 檢查 handler 是否在 3 秒內先 `defer()` 或回覆
- 若是舊訊息按鈕，請確認 custom_id 路由仍相容

### 2. OAuth2 `invalid redirect_uri`

- 確認 Discord Developer Portal 設定了：`{WEB_BASE_URL}/auth/callback`
- 檢查 `.env` 的 `WEB_BASE_URL` 與實際網址一致

### 3. /accept-approver 顯示成功但沒成為核准者

- 需在 DM 中點擊驗證按鈕才會寫入 `recovery_approvers`
- 若收不到 DM，可按「重新發送」並確認 Discord 私訊設定

### 4. 面板資料顯示不正確

- 先確認 bot 已重啟並載入最新 cog
- 查詢日誌中 `cogs.web`、`cogs.monitoring` 的錯誤訊息

## 建議排查順序

1. 看應用啟動 log（cog 是否全部載入）
2. 重現操作並記錄時間
3. 以時間點過濾 `logs/` 與 Redis `bot:logs`
4. 對照 request_id / guild_id / user_id 追蹤整條流程