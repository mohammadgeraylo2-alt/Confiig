"""
این اسکریپت رو روی گوشی (Termux) یا کامپیوتر خودت اجرا کن، نه روی Railway.
نسخه‌ی QR-based + تحمل FloodWaitError روی چک‌های بعد از لاگین.

نیازمندی:
    pip install opentele2 --break-system-packages

اجرا:
    python3 local_login.py
"""

import asyncio
from opentele2.tl import TelegramClient
from opentele2.api import API
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


async def main():
    phone = input("شماره تلفن (با کد کشور، مثلا +989123456789): ").strip()
    api = API.TelegramAndroid.Generate(unique_id=phone)
    client = TelegramClient(StringSession(), api=api)
    await client.connect()

    qr_login = await client.qr_login()
    print("این لینک/کیوآر رو با اکانت مقصد اسکن کن (توییلگرام > تنظیمات > دستگاه‌ها > Link Desktop Device):")
    print(qr_login.url)

    try:
        await qr_login.wait(timeout=60)
    except FloodWaitError as e:
        print(f"\nهشدار: بعد از اسکن، تلگرام روی یه چک اضافی FloodWait داد ({e.seconds} ثانیه).")
        print("این طبیعیه و لاگین رو خراب نمی‌کنه؛ داریم مستقیم سشن رو چک می‌کنیم...\n")
    except Exception as e:
        print(f"خطای غیرمنتظره حین انتظار برای اسکن: {e}")
        await client.disconnect()
        return

    # حالا صرف نظر از FloodWait روی GetStateRequest، ببینیم auth key واقعاً معتبر شده یا نه
    try:
        authorized = await client.is_user_authorized()
    except FloodWaitError:
        # حتی اگه این چک هم FloodWait بخوره، session لوکال (روی دیسک همین کلاینت)
        # ممکنه از قبل معتبر شده باشه چون auth key موقع اسکن ثبت میشه، نه موقع GetState.
        authorized = None

    session_string = client.session.save()

    if authorized is False:
        print("لاگین ناموفق بود (سشن authorize نشده). دوباره امتحان کن.")
        await client.disconnect()
        return

    print("\n" + "=" * 60)
    if authorized is None:
        print("نتونستیم مطمئن بشیم authorize شده یا نه (به خاطر FloodWait)،")
        print("ولی این session string رو ذخیره کن و به بات بده — اگه معتبر نبود، بات با /importsession بهت میگه.")
    else:
        me = await client.get_me()
        print(f"لاگین با موفقیت انجام شد: {me.first_name}")
    print("این پیام رو دقیقاً همین‌جوری (کامل) به ربات ادمین بفرست:\n")
    print(f"/importsession {phone} {session_string}")
    print("=" * 60)

    await client.disconnect()


if __name__ == "__main__":
    asynci
    o.run(main())
