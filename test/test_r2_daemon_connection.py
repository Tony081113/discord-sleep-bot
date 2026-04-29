import os
import sys
import asyncio
import aiohttp
from dotenv import load_dotenv

# 支援直接在 test/ 下執行
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

load_dotenv()

def get_env(name, default=None):
    v = os.getenv(name, default)
    if not v:
        print(f"[ERROR] 缺少環境變數: {name}")
        sys.exit(1)
    return v

async def test_handshake():
    base_url = get_env("DBMOD_DAEMON_URL", "http://127.0.0.1:8091").rstrip("/")
    dbmod_token = get_env("DBMOD_DAEMON_TOKEN")
    expected_daemon_token = get_env("DBMOD_EXPECT_DAEMON_TOKEN")
    url = f"{base_url}/v1/handshake"
    headers = {"Content-Type": "application/json", "X-DBMOD-TOKEN": dbmod_token}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json={}) as resp:
            print(f"Status: {resp.status}")
            body = await resp.json(content_type=None)
            print("Response:", body)
            daemon_token = resp.headers.get("X-DAEMON-TOKEN", "")
            if daemon_token != expected_daemon_token:
                print(f"[FAIL] X-DAEMON-TOKEN 不符: {daemon_token}")
                sys.exit(2)
            if resp.status == 200 and body.get("ok"):
                print("[PASS] Handshake 成功，連線與 token 驗證正常")
            else:
                print("[FAIL] Handshake 失敗")
                sys.exit(3)

if __name__ == "__main__":
    asyncio.run(test_handshake())
