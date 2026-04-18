"""Dump raw MESSAGE_CREATE JSON from a target guild.

Usage:
1. Put DISCORD_TOKEN in .env
2. Run: python test/dump_message_author_raw.py
"""
"""將目標伺服器的 MESSAGE_CREATE 原始 JSON 傾倒出來。

用法:
1. 將 DISCORD_TOKEN 放在 .env 中
2. 執行: python test/dump_message_author_raw.py
"""
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import discord
from dotenv import load_dotenv

TARGET_GUILD_ID = 1493561394422087743
OUTPUT_PATH = Path(__file__).resolve().parent / "output_authors.jsonl"


def _append_jsonl(record: dict) -> None:
    with OUTPUT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is missing in environment or .env")

    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True

    client = discord.Client(intents=intents)
    seen_message_ids: set[str] = set()
    parser_patched = False

    def _handle_message_create(data: dict, source: str) -> None:
        guild_id = data.get("guild_id")
        if str(guild_id) != str(TARGET_GUILD_ID):
            return

        message_id = str(data.get("id") or "")
        if message_id:
            if message_id in seen_message_ids:
                return
            seen_message_ids.add(message_id)
            if len(seen_message_ids) > 20000:
                seen_message_ids.clear()

        author_raw = data.get("author")

        record = {
            "ts": _now_iso(),
            "source": source,
            "guild_id": data.get("guild_id"),
            "channel_id": data.get("channel_id"),
            "message_id": data.get("id"),
            "author_raw": author_raw,
            "message_create_raw": data,
        }
        _append_jsonl(record)

        print("=" * 60, flush=True)
        print(f"[RAW MESSAGE_CREATE author] source={source}", flush=True)
        print(json.dumps(author_raw, ensure_ascii=False, indent=2), flush=True)
        print("=" * 60, flush=True)

    @client.event
    async def on_ready() -> None:
        nonlocal parser_patched
        print(f"[READY] Logged in as {client.user} (id={client.user.id})", flush=True)
        print(f"[INFO] Filtering guild_id={TARGET_GUILD_ID}", flush=True)
        print(f"[INFO] Writing JSONL to {OUTPUT_PATH}", flush=True)
        guild_ids = [str(g.id) for g in client.guilds]
        print(f"[INFO] Connected guilds={guild_ids}", flush=True)
        if str(TARGET_GUILD_ID) not in guild_ids:
            print("[WARN] Target guild not found in current bot guild list", flush=True)
        print("[INFO] Listening with on_socket_response + on_socket_raw_receive", flush=True)

        if not parser_patched:
            parsers = getattr(client._connection, "parsers", None)
            if isinstance(parsers, dict):
                original = parsers.get("MESSAGE_CREATE")
                if callable(original):
                    def _patched_parser(data: dict) -> None:
                        if isinstance(data, dict):
                            _handle_message_create(data, source="connection.parsers.MESSAGE_CREATE")
                        original(data)

                    parsers["MESSAGE_CREATE"] = _patched_parser
                    parser_patched = True
                    print("[INFO] Patched internal MESSAGE_CREATE parser", flush=True)
                else:
                    print("[WARN] Internal MESSAGE_CREATE parser not found", flush=True)
            else:
                print("[WARN] Internal parser map unavailable", flush=True)

    @client.event
    async def on_socket_response(payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("t") != "MESSAGE_CREATE":
            return
        data = payload.get("d") or {}
        _handle_message_create(data, source="on_socket_response")

    @client.event
    async def on_socket_raw_receive(msg: Any) -> None:
        if isinstance(msg, bytes):
            try:
                msg = msg.decode("utf-8")
            except Exception:
                return
        if not isinstance(msg, str):
            return

        try:
            payload = json.loads(msg)
        except json.JSONDecodeError:
            return

        if payload.get("t") != "MESSAGE_CREATE":
            return

        data = payload.get("d") or {}
        if isinstance(data, dict):
            _handle_message_create(data, source="on_socket_raw_receive")

    client.run(token)


if __name__ == "__main__":
    main()
