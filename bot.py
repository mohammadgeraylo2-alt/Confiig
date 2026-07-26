"""
ربات ادمین برای مدیریت چند سلف‌بات تلگرام
--------------------------------------------
با این ربات میتونی از توی خود تلگرام:
  - شماره تلفن اکانت‌های جدید رو بدی و باهاش لاگین کنی (مثل لاگین دستی، ولی از طریق چت ربات)
  - گروه مقصد و فاصله زمانی رو تنظیم کنی
  - لیست اکانت‌ها رو ببینی، استارت/استاپ/حذف کنی

فقط خود تو (ADMIN_ID) میتونی به این ربات دستور بدی.

نیازمندی:
    pip install python-telegram-bot telethon --break-system-packages

Environment Variables لازم:
    BOT_TOKEN  -> توکن ربات از @BotFather
    ADMIN_ID   -> آیدی عددی تلگرام خودت (از @userinfobot بگیر)
    API_ID     -> از my.telegram.org
    API_HASH   -> از my.telegram.org
"""

import asyncio
import json
import logging
import os
import random
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

DATA_FILE = Path("accounts.json")
PHONE, CODE, PASSWORD = range(3)

pending = {}                        # user_id -> اطلاعات موقت هنگام لاگین
running = {}                        # label -> {"client":..., "task":...}
settings = {"group": None, "message": "میو", "min_delay": 300, "max_delay": 360}


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"accounts": {}, "settings": dict(settings)}


def save_data():
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


data = load_data()
settings.update(data.get("settings", {}))


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# افزودن اکانت جدید (لاگین با شماره تلفن از طریق چت ربات)
# ---------------------------------------------------------------------------
@admin_only
async def addaccount_start(update, context):
    await update.message.reply_text(
        "شماره تلفن اکانتی که میخوای اضافه کنی رو بفرست (با کد کشور، مثلا +989123456789):"
    )
    return PHONE


async def addaccount_phone(update, context):
    phone = update.message.text.strip()
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await update.message.reply_text(f"خطا در ارسال کد: {e}")
        await client.disconnect()
        return ConversationHandler.END

    pending[update.effective_user.id] = {
        "client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash,
    }
    await update.message.reply_text("کدی که تلگرام فرستاد رو بفرست:")
    return CODE


async def addaccount_code(update, context):
    info = pending.get(update.effective_user.id)
    code = update.message.text.strip()
    client = info["client"]
    try:
        await client.sign_in(info["phone"], code, phone_code_hash=info["phone_code_hash"])
    except SessionPasswordNeededError:
        await update.message.reply_text("این اکانت رمز دومرحله‌ای داره، رمزشو بفرست:")
        return PASSWORD
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        await client.disconnect()
        pending.pop(update.effective_user.id, None)
        return ConversationHandler.END

    return await finish_login(update, context)


async def addaccount_password(update, context):
    info = pending.get(update.effective_user.id)
    client = info["client"]
    password = update.message.text.strip()
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await update.message.reply_text(f"خطا: {e}")
        await client.disconnect()
        pending.pop(update.effective_user.id, None)
        return ConversationHandler.END

    return await finish_login(update, context)


async def finish_login(update, context):
    info = pending.pop(update.effective_user.id)
    client = info["client"]
    me = await client.get_me()
    label = info["phone"]

    data["accounts"][label] = {"session": client.session.save(), "active": True}
    save_data()

    await update.message.reply_text(f"اکانت {me.first_name} ({label}) با موفقیت اضافه شد.")

    if settings["group"]:
        start_worker(label, client)
    else:
        await client.disconnect()
        await update.message.reply_text(
            "گروه هنوز تنظیم نشده. با /setgroup تنظیمش کن تا اکانت‌ها شروع به کار کنن."
        )
    return ConversationHandler.END


async def cancel(update, context):
    info = pending.pop(update.effective_user.id, None)
    if info:
        await info["client"].disconnect()
    await update.message.reply_text("لغو شد.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ارسال دوره‌ای پیام برای هر اکانت
# ---------------------------------------------------------------------------
def start_worker(label, client):
    task = asyncio.create_task(worker_loop(label, client))
    running[label] = {"client": client, "task": task}


async def worker_loop(label, client):
    if not client.is_connected():
        await client.connect()
    while True:
        try:
            await client.send_message(settings["group"], settings["message"])
            logger.info(f"[{label}] پیام ارسال شد")
        except Exception as e:
            logger.warning(f"[{label}] خطا: {e}")
        delay = random.randint(settings["min_delay"], settings["max_delay"])
        await asyncio.sleep(delay)


async def start_all_accounts():
    if not settings["group"]:
        return
    for label, acc in data["accounts"].items():
        if not acc.get("active", True) or label in running:
            continue
        client = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH)
        await client.connect()
        start_worker(label, client)


# ---------------------------------------------------------------------------
# دستورات مدیریتی
# ---------------------------------------------------------------------------
@admin_only
async def cmd_setgroup(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /setgroup @username یا آیدی گروه")
        return
    settings["group"] = context.args[0]
    data["settings"] = settings
    save_data()
    await update.message.reply_text(f"گروه تنظیم شد: {settings['group']}")
    await start_all_accounts()


@admin_only
async def cmd_setinterval(update, context):
    try:
        mn, mx = int(context.args[0]), int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("استفاده: /setinterval 300 360   (بر حسب ثانیه)")
        return
    settings["min_delay"], settings["max_delay"] = mn, mx
    data["settings"] = settings
    save_data()
    await update.message.reply_text(f"فاصله زمانی تنظیم شد: {mn} تا {mx} ثانیه")


@admin_only
async def cmd_setmessage(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /setmessage متن پیام")
        return
    settings["message"] = " ".join(context.args)
    data["settings"] = settings
    save_data()
    await update.message.reply_text(f"پیام تنظیم شد: {settings['message']}")


@admin_only
async def cmd_list(update, context):
    if not data["accounts"]:
        await update.message.reply_text("هیچ اکانتی اضافه نشده.")
        return
    lines = [f"{label} — {'فعال' if label in running else 'متوقف'}" for label in data["accounts"]]
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_stop(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /stopaccount شماره_تلفن")
        return
    label = context.args[0]
    info = running.pop(label, None)
    if info:
        info["task"].cancel()
        await info["client"].disconnect()
        data["accounts"][label]["active"] = False
        save_data()
        await update.message.reply_text(f"{label} متوقف شد.")
    else:
        await update.message.reply_text("این اکانت در حال اجرا نیست.")


@admin_only
async def cmd_start_account(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /startaccount شماره_تلفن")
        return
    label = context.args[0]
    if label in running:
        await update.message.reply_text("از قبل در حال اجراست.")
        return
    acc = data["accounts"].get(label)
    if not acc:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return
    client = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH)
    await client.connect()
    start_worker(label, client)
    data["accounts"][label]["active"] = True
    save_data()
    await update.message.reply_text(f"{label} استارت شد.")


@admin_only
async def cmd_remove(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /removeaccount شماره_تلفن")
        return
    label = context.args[0]
    info = running.pop(label, None)
    if info:
        info["task"].cancel()
        await info["client"].disconnect()
    data["accounts"].pop(label, None)
    save_data()
    await update.message.reply_text(f"{label} حذف شد.")


@admin_only
async def cmd_start(update, context):
    await update.message.reply_text(
        "دستورات:\n"
        "/addaccount - اضافه کردن اکانت جدید با شماره تلفن\n"
        "/setgroup - تنظیم گروه مقصد\n"
        "/setinterval min max - تنظیم فاصله زمانی (ثانیه)\n"
        "/setmessage - تنظیم متن پیام\n"
        "/list - لیست اکانت‌ها\n"
        "/stopaccount شماره - متوقف کردن یک اکانت\n"
        "/startaccount شماره - استارت یک اکانت\n"
        "/removeaccount شماره - حذف کامل یک اکانت"
    )


async def post_init(app):
    await start_all_accounts()


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("addaccount", addaccount_start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addaccount_phone)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addaccount_code)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, addaccount_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("setmessage", cmd_setmessage))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stopaccount", cmd_stop))
    app.add_handler(CommandHandler("startaccount", cmd_start_account))
    app.add_handler(CommandHandler("removeaccount", cmd_remove))

    app.run_polling()


if __name__ == "__main__":
    main()
