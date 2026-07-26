"""
این اسکریپت رو روی گوشی (Termux) یا کامپیوتر خودت اجرا کن، نه روی Railway.
چون لاگین کردن باید از یه شبکه عادی (نه دیتاسنتر/کلود) انجام بشه وگرنه
تلگرام درخواست reCAPTCHA میده که هیچ اسکریپتی نمی‌تونه حلش کنه.

نیازمندی:
    pip install opentele2 --break-system-packages

اجرا:
    python3 local_login.py
"""

import asyncio
from opentele2.tl import TelegramClient
from opentele2.api import API
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError


async def main():
    phone = input("شماره تلفن (با کد کشور، مثلا +989123456789): ").strip()

    api = API.TelegramAndroid.Generate(unique_id=phone)
    client = TelegramClient(StringSession(), api=api)
    await client.connect()

    sent = await client.send_code_request(phone)
    code = input("کدی که تلگرام فرستاد: ").strip()

    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        password = input("رمز دومرحله‌ای: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    session_string = client.session.save()

    print("\n" + "=" * 60)
    print(f"لاگین با موفقیت انجام شد: {me.first_name}")
    print("این پیام رو دقیقاً همین‌جوری (کامل) به ربات ادمین بفرست:\n")
    print(f"/importsession {phone} {session_string}")
    print("=" * 60)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
