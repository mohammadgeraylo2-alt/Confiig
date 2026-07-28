"""
ربات ادمین برای مدیریت چند سلف‌بات تلگرام
--------------------------------------------
این نسخه از کتابخونه opentele2 استفاده می‌کنه که API رسمی اپ اندروید تلگرام
رو همراه خودش داره، پس دیگه لازم نیست از my.telegram.org چیزی بگیری.

فقط خود تو (ADMIN_ID) میتونی به این ربات دستور بدی.

نیازمندی:
    pip install python-telegram-bot opentele2 --break-system-packages
    pip install uvloop --break-system-packages   # اختیاری، برای سرعت بیشتر کلیک نجات میو

Environment Variables لازم:
    BOT_TOKEN  -> توکن ربات از @BotFather
    ADMIN_ID   -> آیدی عددی تلگرام خودت (از @userinfobot بگیر)
"""

import asyncio
import functools
import json
import logging
import os
import random
from pathlib import Path

from opentele2.tl import TelegramClient
from opentele2.api import API
from telethon import events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

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

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "accounts.json"
PHONE, CODE, PASSWORD = range(3)

pending = {}                        # user_id -> اطلاعات موقت هنگام لاگین
running = {}                        # label -> {"client":..., "task":...}
rescue_handlers = {}                # label -> {"client":..., "new":..., "edited":...}
settings = {
    "group": None, "message": "میو", "min_delay": 300, "max_delay": 360,
    "rescue_account": None,         # فقط همین اکانت دکمه "نجات دادن میو" رو کلیک می‌کنه
}

RESCUE_KEYWORDS = ["نجات دادن میو", "نجات میو", "نجات"]


def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text())
    return {"accounts": {}, "settings": dict(settings)}


def save_data():
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def make_api(label: str):
    # unique_id ثابت برای هر اکانت یعنی دیوایس‌فینگرپرینت همیشه یکسان تولید میشه
    return API.TelegramAndroid.Generate(unique_id=label)


data = load_data()
settings.update(data.get("settings", {}))


def admin_only(func):
    @functools.wraps(func)
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
    api = make_api(phone)
    client = TelegramClient(StringSession(), api=api)
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

    def _on_done(t, label=label):
        # اگه تسک به هر دلیلی (کرش یا کنسل) تموم شد، از running پاکش کن
        # تا /list وضعیت واقعی رو نشون بده، نه یه اکانت مرده که "فعال" نمایش داده میشه
        if label in running and running[label]["task"] is t:
            running.pop(label, None)
        detach_rescue_handler(label)
        if not t.cancelled() and t.exception():
            logger.error(f"[{label}] worker متوقف شد (خطای غیرمنتظره): {t.exception()}")

    task.add_done_callback(_on_done)

    if label == settings.get("rescue_account"):
        attach_rescue_handler(label, client)


async def worker_loop(label, client):
    if not client.is_connected():
        await client.connect()
    while True:
        try:
            group = settings["group"]
            # آیدی عددی گروه (مثلا -1001234567890) باید int باشه وگرنه
            # تلگرام نمیتونه entity رو resolve کنه و ارسال همیشه فیل میشه
            if isinstance(group, str) and group.lstrip("-").isdigit():
                group = int(group)
            await client.send_message(group, settings["message"])
            logger.info(f"[{label}] پیام ارسال شد")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا: {e}")

        try:
            mn, mx = settings["min_delay"], settings["max_delay"]
            if mn > mx:
                mn, mx = mx, mn  # جلوگیری از کرش اگه من/مکس اشتباه ست شده باشن
            delay = random.randint(mn, mx)
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا در محاسبه تاخیر: {e}")
            await asyncio.sleep(300)  # مقدار پیش‌فرض امن


def _button_matches_rescue(button):
    text = button.text or ""
    return any(keyword in text for keyword in RESCUE_KEYWORDS)


async def _try_click_rescue_button(event, label, get_peer, client):
    if not event.buttons:
        return
    for row in event.buttons:
        for button in row:
            if _button_matches_rescue(button):
                data = getattr(button.button, "data", None)
                peer = await get_peer()

                async def _one_click(attempt):
                    try:
                        if data is not None and peer is not None:
                            # تماس مستقیم raw API با peer از قبل کش‌شده، بدون
                            # resolve دوباره entity در لحظه کلیک -> چند ده میلی‌ثانیه سریع‌تر
                            await client(GetBotCallbackAnswerRequest(
                                peer=peer, msg_id=event.id, data=data,
                            ))
                        else:
                            await button.click()  # fallback مطمئن اگه چیزی کش نشده بود
                        logger.info(f"[{label}] کلیک #{attempt} روی '{button.text}' انجام شد (نجات میو)")
                    except Exception as e:
                        logger.warning(f"[{label}] خطا در کلیک #{attempt}: {e}")

                # هر ۳ کلیک تقریبا همزمان و بدون فاصله فرستاده میشن تا سریع‌ترین حالت ممکن باشه
                await asyncio.gather(
                    _one_click(1), _one_click(2), _one_click(3),
                    return_exceptions=True,
                )
                return


def _resolve_group(group):
    if isinstance(group, str) and group.lstrip("-").isdigit():
        return int(group)
    return group


def detach_rescue_handler(label):
    info = rescue_handlers.pop(label, None)
    if not info:
        return
    client = info["client"]
    client.remove_event_handler(info["new"])
    client.remove_event_handler(info["edited"])


def attach_rescue_handler(label, client):
    detach_rescue_handler(label)  # جلوگیری از هندلر تکراری روی همون اکانت
    group = settings["group"]
    if not group:
        return
    group = _resolve_group(group)

    peer_cache = {"peer": None}

    async def _get_peer():
        # فقط اولین بار واقعا به سرور درخواست میزنه، بعدش از کش برمیگرده
        if peer_cache["peer"] is None:
            peer_cache["peer"] = await client.get_input_entity(group)
        return peer_cache["peer"]

    # همین الان (نه لحظه اومدن پیام نجات) peer رو پیش‌واکشی کن
    asyncio.create_task(_get_peer())

    async def _on_new(event, label=label):
        await _try_click_rescue_button(event, label, _get_peer, client)

    async def _on_edited(event, label=label):
        await _try_click_rescue_button(event, label, _get_peer, client)

    client.add_event_handler(_on_new, events.NewMessage(chats=group))
    client.add_event_handler(_on_edited, events.MessageEdited(chats=group))
    rescue_handlers[label] = {"client": client, "new": _on_new, "edited": _on_edited}
    logger.info(f"[{label}] بعنوان اکانت نجات میو تنظیم شد")


def apply_rescue_account():
    # فقط یه اکانت باید نجات‌دهنده باشه، پس اول همه هندلرهای قبلی رو پاک می‌کنیم
    for lbl in list(rescue_handlers.keys()):
        detach_rescue_handler(lbl)
    label = settings.get("rescue_account")
    if not label:
        return
    info = running.get(label)
    if info:
        attach_rescue_handler(label, info["client"])


async def start_all_accounts():
    if not settings["group"]:
        return
    for label, acc in data["accounts"].items():
        if not acc.get("active", True) or label in running:
            continue
        client = TelegramClient(StringSession(acc["session"]), api=make_api(label))
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
    group = context.args[0]
    if group.lstrip("-").isdigit():
        group = int(group)
    settings["group"] = group
    data["settings"] = settings
    save_data()
    await update.message.reply_text(f"گروه تنظیم شد: {settings['group']}")
    await start_all_accounts()
    apply_rescue_account()


@admin_only
async def cmd_setinterval(update, context):
    try:
        mn, mx = int(context.args[0]), int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("استفاده: /setinterval 300 360   (بر حسب ثانیه)")
        return
    if mn <= 0 or mx <= 0:
        await update.message.reply_text("مقادیر باید بزرگتر از صفر باشن.")
        return
    if mn > mx:
        mn, mx = mx, mn  # خودکار جابجا میشه به‌جای کرش کردن worker
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
async def cmd_setrescuer(update, context):
    if not context.args:
        current = settings.get("rescue_account") or "هیچکدوم"
        await update.message.reply_text(
            f"اکانت نجات‌دهنده فعلی: {current}\n"
            "استفاده: /setrescuer شماره_تلفن   (برای غیرفعال کردن: /setrescuer off)"
        )
        return

    label = context.args[0]
    if label.lower() == "off":
        settings["rescue_account"] = None
        data["settings"] = settings
        save_data()
        apply_rescue_account()
        await update.message.reply_text("اکانت نجات‌دهنده غیرفعال شد.")
        return

    if label not in data["accounts"]:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return

    settings["rescue_account"] = label
    data["settings"] = settings
    save_data()
    apply_rescue_account()

    if label in running:
        await update.message.reply_text(
            f"اکانت {label} بعنوان نجات‌دهنده میو تنظیم شد و همین الان فعاله."
        )
    else:
        await update.message.reply_text(
            f"اکانت {label} بعنوان نجات‌دهنده میو تنظیم شد، ولی چون در حال اجرا نیست "
            f"اول با /startaccount {label} روشنش کن."
        )


@admin_only
async def cmd_list(update, context):
    if not data["accounts"]:
        await update.message.reply_text("هیچ اکانتی اضافه نشده.")
        return
    lines = []
    for label in data["accounts"]:
        status = "فعال" if label in running else "متوقف"
        tag = " 🐱 نجات‌دهنده" if label == settings.get("rescue_account") else ""
        lines.append(f"{label} — {status}{tag}")
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
        detach_rescue_handler(label)
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
    client = TelegramClient(StringSession(acc["session"]), api=make_api(label))
    await client.connect()
    start_worker(label, client)
    data["accounts"][label]["active"] = True
    save_data()
    await update.message.reply_text(f"{label} استارت شد.")


@admin_only
async def cmd_import(update, context):
    if len(context.args) < 2:
        await update.message.reply_text(
            "استفاده: /importsession شماره_تلفن session_string\n"
            "(این استرینگ رو از اجرای local_login.py روی گوشی/کامپیوتر خودت میگیری)"
        )
        return
    label = context.args[0]
    session_string = context.args[1]

    client = TelegramClient(StringSession(session_string), api=make_api(label))
    await client.connect()
    try:
        me = await client.get_me()
    except Exception as e:
        await update.message.reply_text(f"این session معتبر نیست: {e}")
        await client.disconnect()
        return

    data["accounts"][label] = {"session": session_string, "active": True}
    save_data()
    await update.message.reply_text(f"اکانت {me.first_name} ({label}) اضافه شد.")

    if settings["group"]:
        start_worker(label, client)
    else:
        await client.disconnect()
        await update.message.reply_text("گروه هنوز تنظیم نشده. با /setgroup تنظیمش کن.")


@admin_only
async def cmd_remove(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /removeaccount شماره_تلفن")
        return
    label = context.args[0]
    info = running.pop(label, None)
    if info:
        info["task"].cancel()
        detach_rescue_handler(label)
        await info["client"].disconnect()
    data["accounts"].pop(label, None)
    if settings.get("rescue_account") == label:
        settings["rescue_account"] = None
        data["settings"] = settings
    save_data()
    await update.message.reply_text(f"{label} حذف شد.")


@admin_only
async def cmd_start(update, context):
    await update.message.reply_text(
        "دستورات:\n"
        "/addaccount - اضافه کردن اکانت جدید با شماره تلفن\n"
        "/importsession شماره session_string - اضافه کردن اکانتی که لوکال لاگین کردی\n"
        "/setgroup - تنظیم گروه مقصد\n"
        "/setinterval min max - تنظیم فاصله زمانی (ثانیه)\n"
        "/setmessage - تنظیم متن پیام\n"
        "/setrescuer شماره - انتخاب اکانتی که خودکار دکمه «نجات دادن میو» رو کلیک کنه (off برای غیرفعال کردن)\n"
        "/list - لیست اکانت‌ها\n"
        "/stopaccount شماره - متوقف کردن یک اکانت\n"
        "/startaccount شماره - استارت یک اکانت\n"
        "/removeaccount شماره - حذف کامل یک اکانت"
    )


async def post_init(app):
    await start_all_accounts()


def main():
    try:
        import uvloop
        uvloop.install()
        logger.info("uvloop فعال شد (event loop سریع‌تر)")
    except ImportError:
        pass  # مشکلی نیست، بدون uvloop هم کار میکنه فقط یه ذره کندتر

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
    app.add_handler(CommandHandler("importsession", cmd_import))
    app.add_handler(CommandHandler("setrescuer", cmd_setrescuer))

    app.run_polling()


if __name__ == "__main__":
    main()
