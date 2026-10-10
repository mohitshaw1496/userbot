import asyncio
import os
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    async with TelegramClient(
        StringSession(),
        api_id,
        api_hash
    ) as client:
        print("\nYour StringSession:")
        print(client.session.save())
        print("\nKeep this secret. Anyone with it may access your account.")

if __name__ == "__main__":
    asyncio.run(main())
