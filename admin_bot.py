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
import re
from pathlib import Path

from opentele2.tl import TelegramClient
from opentele2.api import API
from telethon import events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.messages import GetBotCallbackAnswerRequest
from telethon.tl.types import InputMediaDice

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
    "rescue_account": None,   # فقط همین اکانت دکمه "نجات دادن میو" رو کلیک می‌کنه
    "fisher_account": None,   # فقط همین اکانت روتین ماهیگیری/یخچال رو انجام میده
    "smuggler_account": None, # فقط همین اکانت روتین قاچاق میویی رو انجام میده
    "casino_account": None,   # فقط همین اکانت هر ۶ دقیقه "کازینو" رو می‌فرسته
    "pishi_account": None,    # فقط همین اکانت هر ۶ ساعت "پیشی" رو می‌فرسته و برداشت می‌کنه
    "casino_stats": {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0},
    "streak_casino_account": None,  # فقط همین اکانت روتین «تاس بر اساس رشته فرد/زوج» رو انجام میده
    "streak_casino_stats": {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0},
    "streak_state": {},        # label -> {"streak_len", "last_is_even", "total_thrown"} - برای زنده موندن بعد از ری‌استارت
    "streak_dice_chat": None,  # کانالی که اکانت توش ادمینه و تاس‌های شمارش رشته رو اونجا میندازه (None یعنی سیو مسیج)
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
        stop_fishing(label)
        stop_smuggling(label)
        stop_casino(label)
        stop_pishi(label)
        stop_dice_streak(label)
        stop_streak_casino(label)
        if not t.cancelled() and t.exception():
            logger.error(f"[{label}] worker متوقف شد (خطای غیرمنتظره): {t.exception()}")

    task.add_done_callback(_on_done)

    if label == settings.get("rescue_account"):
        attach_rescue_handler(label, client)
    if label == settings.get("fisher_account"):
        start_fishing(label, client)
    if label == settings.get("smuggler_account"):
        start_smuggling(label, client)
    if label == settings.get("casino_account"):
        start_casino(label, client)
    if label == settings.get("pishi_account"):
        start_pishi(label, client)
    if label == settings.get("streak_casino_account"):
        start_streak_casino(label, client)


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
    text = _clean_text(button.text or "")
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


async def wait_for_event(client, group, predicate, timeout=30, edited=False):
    """منتظر میمونه تا یه پیام (جدید یا ادیت‌شده) که predicate روش True برگردونه بیاد."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def handler(event):
        try:
            if not future.done() and predicate(event):
                future.set_result(event)
        except Exception:
            pass

    ev_type = events.MessageEdited(chats=group) if edited else events.NewMessage(chats=group)
    client.add_event_handler(handler, ev_type)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        client.remove_event_handler(handler, ev_type)


async def wait_step(client, group, predicate, desc, label, timeout=30, edited=False):
    """مثل wait_for_event ولی مرحله رو قبل و بعد لاگ می‌کنه تا دقیقا معلوم بشه کجا گیر کرد."""
    try:
        result = await wait_for_event(client, group, predicate, timeout=timeout, edited=edited)
        logger.info(f"[{label}] ✓ رسید: {desc}")
        return result
    except asyncio.TimeoutError:
        logger.warning(f"[{label}] ⏱ تایم‌اوت ({timeout}s) در انتظار: {desc}")
        raise


async def act_and_wait(client, group, action, predicate, desc, label, timeout=30, edited=False):
    """دقیقا همون الگوی نجات میو: اول handler رو ثبت می‌کنه (شروع گوش دادن)،
    و فقط بعدش action (ارسال پیام یا کلیک روی دکمه) رو اجرا می‌کنه.

    فرقش با wait_step این بود که wait_step *بعد* از ارسال پیام صدا زده می‌شد،
    یعنی یه فاصله (هرچند خیلی کوچیک) بین فرستادن پیام و ثبت شدن listener وجود
    داشت. اگه جواب بازی سریع‌تر از این فاصله می‌رسید (مخصوصا رو ماهی که جوابش
    تقریبا آنی‌ه)، اون پیام از دست می‌رفت و کد تا timeout منتظر می‌موند بدون
    اینکه هیچ‌وقت ببینتش. توی نجات میو این مشکل نبود چون handlerش از اول به
    صورت دائمی (always-on) روشنه و هیچ‌وقت خاموش نمی‌شه.

    اینجا با ثبت‌کردن handler *قبل* از اجرای action، همون تضمین رو داریم: هر
    پیامی که در پاسخ بیاد -- حتی اگه در عرض چند میلی‌ثانیه بعد از send برسه --
    حتما گرفته می‌شه.
    """
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def handler(event):
        try:
            if not future.done() and predicate(event):
                future.set_result(event)
        except Exception:
            pass

    ev_type = events.MessageEdited(chats=group) if edited else events.NewMessage(chats=group)
    client.add_event_handler(handler, ev_type)
    try:
        await action()
        result = await asyncio.wait_for(future, timeout=timeout)
        logger.info(f"[{label}] ✓ رسید: {desc}")
        return result
    except asyncio.TimeoutError:
        logger.warning(f"[{label}] ⏱ تایم‌اوت ({timeout}s) در انتظار: {desc}")
        await _debug_dump_recent_buttons(client, group, label, desc)
        raise
    finally:
        client.remove_event_handler(handler, ev_type)


async def _debug_dump_recent_buttons(client, group, label, context_desc):
    """وقتی منتظر یه دکمه/پیام خاص می‌مونیم و تایم‌اوت می‌خوریم، آخرین چند پیام
    گروه رو می‌خونه و متن دقیق پیام و دکمه‌هاشون رو با repr() لاگ می‌کنه (که
    کاراکترهای نامرئی/ترکیبی هم توش دیده بشن). این کمک می‌کنه بفهمیم بازی دقیقا
    چه چیزی فرستاده و چرا predicate باهاش match نکرده."""
    try:
        async for msg in client.iter_messages(group, limit=5):
            if msg.buttons:
                rows = [[repr(b.text) for b in row] for row in msg.buttons]
                logger.warning(f"[{label}] دیباگ [{context_desc}] پیام {msg.id}: دکمه‌ها={rows}")
            elif msg.raw_text:
                logger.warning(f"[{label}] دیباگ [{context_desc}] پیام {msg.id}: متن={_clean_text(msg.raw_text)!r}")
    except Exception as e:
        logger.warning(f"[{label}] خطا در دیباگ دامپ پیام‌ها: {e}")


async def wait_step_any(client, group, predicate, desc, label, timeout=30):
    """مثل wait_step ولی هم‌زمان به پیام جدید و هم به ادیت‌شدن پیام گوش می‌ده -
    دقیقا همون الگویی که تو کازینو مشکل «پیام رفت ولی روی دکمه شیشه‌ای کلیک
    نشد» رو حل کرد (چون بازی گاهی جواب رو با ادیت همون پیام قبلی می‌ده، نه
    پیام جدا، و wait_step معمولی فقط یکی از این دوتا رو چک می‌کرد)."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def handler(event):
        try:
            if not future.done() and predicate(event):
                future.set_result(event)
        except Exception:
            pass

    new_ev = events.NewMessage(chats=group)
    edited_ev = events.MessageEdited(chats=group)
    client.add_event_handler(handler, new_ev)
    client.add_event_handler(handler, edited_ev)
    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        logger.info(f"[{label}] ✓ رسید: {desc}")
        return result
    except asyncio.TimeoutError:
        logger.warning(f"[{label}] ⏱ تایم‌اوت ({timeout}s) در انتظار: {desc}")
        await _debug_dump_recent_buttons(client, group, label, desc)
        raise
    finally:
        client.remove_event_handler(handler, new_ev)
        client.remove_event_handler(handler, edited_ev)


async def act_and_wait_any(client, group, action, predicate, desc, label, timeout=30):
    """مثل act_and_wait ولی هم‌زمان به پیام جدید و هم به ادیت‌شدن پیام گوش می‌ده.
    توی روتین کازینو، بعضی مراحل (مثلا درخواست وارد کردن مبلغ، یا به‌روزرسانی پنل
    بعد از تعیین مبلغ/تعداد بازیکن) گاهی با ادیت همون پیام قبلی جواب داده می‌شن،
    نه پیام جدا. به همین خاطر اینجا هر دو نوع event رو هم‌زمان با هم چک می‌کنیم
    تا مهم نباشه بازی کدومش رو انتخاب می‌کنه."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    async def handler(event):
        try:
            if not future.done() and predicate(event):
                future.set_result(event)
        except Exception:
            pass

    new_ev = events.NewMessage(chats=group)
    edited_ev = events.MessageEdited(chats=group)
    client.add_event_handler(handler, new_ev)
    client.add_event_handler(handler, edited_ev)
    try:
        await action()
        result = await asyncio.wait_for(future, timeout=timeout)
        logger.info(f"[{label}] ✓ رسید: {desc}")
        return result
    except asyncio.TimeoutError:
        logger.warning(f"[{label}] ⏱ تایم‌اوت ({timeout}s) در انتظار: {desc}")
        await _debug_dump_recent_buttons(client, group, label, desc)
        raise
    finally:
        client.remove_event_handler(handler, new_ev)
        client.remove_event_handler(handler, edited_ev)


# تلگرام گاهی بین کلمات فارسی (خصوصا کنار ایموجی/عدد) کاراکترهای نامرئی
# جهت‌دهی (RTL/LTR mark و مشابهش) اضافه می‌کنه که تو صفحه دیده نمیشن ولی
# تو متن خام هستن و باعث میشن "in" چک متن رو fail کنه. اینا رو حذف می‌کنیم.
_INVISIBLE_CHARS_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff]"
)


def _clean_text(s):
    if not s:
        return s
    return _INVISIBLE_CHARS_RE.sub("", s)


def _find_button_containing(event, keyword):
    if not event or not event.buttons:
        return None
    for row in event.buttons:
        for b in row:
            if keyword in _clean_text(b.text or ""):
                return b
    return None


def _get_button_at(event, row_idx, col_idx):
    """دکمه رو بر اساس موقعیت (ردیف، ستون) برمی‌گردونه، نه متن. لازم شد چون
    دکمه‌های منوی اصلی کازینو از ایموجی سفارشی/پرمیوم تلگرام استفاده می‌کنن و
    تلگرام برای این نوع دکمه‌ها متن قابل‌خوندنی نمی‌ذاره - فقط یه کاراکتر
    نامرئی (zero-width space) برمی‌گردونه. پس تشخیص با متن همیشه fail می‌شه و
    باید بر اساس جایگاه ثابت دکمه توی چیدمان عمل کنیم."""
    if not event or not event.buttons:
        return None
    try:
        return event.buttons[row_idx][col_idx]
    except (IndexError, TypeError):
        return None


def _find_confirm_button(event):
    """دکمه تاییدی (✅) رو برمی‌گردونه - اول با متن، و اگه نبود (چون گاهی این
    دکمه ایموجی سفارشی/پرمیومه و تلگرام متنش رو خالی می‌ذاره، دقیقا مثل
    دکمه‌های منوی کازینو) با شکل ثابت پنل‌هایی که این دکمه توشون میاد:
    یا ۳ ردیف (➖➕➕ / ✅ / BK یعنی ۳+۱+۱)، یا فقط یه ردیف با یه دکمه تنها."""
    btn = _find_button_containing(event, "✅")
    if btn:
        return btn
    if not event or not event.buttons:
        return None
    rows = event.buttons
    if len(rows) == 3 and len(rows[0]) == 3 and len(rows[1]) == 1 and len(rows[2]) == 1:
        return rows[1][0]
    if len(rows) == 1 and len(rows[0]) == 1:
        return rows[0][0]
    return None


def _has_confirm_button(event):
    return _find_confirm_button(event) is not None


def _has_button_containing(event, keyword):
    return _find_button_containing(event, keyword) is not None


# نگاشت ارقام فارسی/عربی به معادل انگلیسی، برای اینکه اگه بازی رقم رو به
# صورت فارسی («۱») هم نشون بده تشخیص داده بشه
_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _find_number_button(event, n):
    """دکمه‌ای که فقط شامل عدد n باشه رو پیدا می‌کنه - چه رقم ساده (1) باشه
    چه ایموجی کیکپ (1️⃣). ایموجی کیکپ از رقم معمولی + دو کاراکتر ترکیبی
    (variation selector U+FE0F و combining enclosing keycap U+20E3) تشکیل شده،
    پس با پاک کردن کاراکترهای نامرئی/ترکیبی و مقایسه فقط رقم‌های باقی‌مونده،
    هر دو حالت درست تشخیص داده می‌شن. همچنین رقم‌های فارسی رو هم پوشش می‌ده."""
    if not event or not event.buttons:
        return None
    target = str(n)
    for row in event.buttons:
        for b in row:
            text = _clean_text(b.text or "").translate(_DIGIT_MAP)
            digits = re.sub(r"[^\d]", "", text)
            if digits == target:
                return b
    return None


FISH_INTERVAL = 46 * 60   # هر ۴۶ دقیقه یه بار درخواست ماهی
COOK_WAIT = 30 * 60       # ۳۰ دقیقه صبر بعد از پخت، قبل از فروش
fishing_tasks = {}        # label -> asyncio.Task


def start_fishing(label, client):
    stop_fishing(label)
    fishing_tasks[label] = asyncio.create_task(fishing_loop(label, client))
    logger.info(f"[{label}] روتین ماهیگیری/یخچال فعال شد")


def stop_fishing(label):
    task = fishing_tasks.pop(label, None)
    if task:
        task.cancel()


async def fishing_loop(label, client):
    while True:
        start_t = asyncio.get_event_loop().time()
        try:
            await run_fishing_cycle(label, client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا توی چرخه ماهیگیری: {type(e).__name__}: {e}")
        elapsed = asyncio.get_event_loop().time() - start_t
        await asyncio.sleep(max(FISH_INTERVAL - elapsed, 60))


async def run_fishing_cycle(label, client):
    group = _resolve_group(settings["group"])
    if not group:
        return

    # ۱. درخواست ماهی
    caught = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "ماهی"),
        lambda e: e.raw_text and "با موفقیت" in _clean_text(e.raw_text) and "گرفتید" in _clean_text(e.raw_text),
        "پیام گرفتن ماهی ('با موفقیت ... گرفتید')", label, timeout=60,
    )
    logger.info(f"[{label}] پیام 'ماهی' ارسال شد")

    # ۲. دکمه "یخچال" معمولا روی همین پیام گرفتن ماهیه (نه پیام جدا)
    btn = _find_button_containing(caught, "یخچال")
    if btn:
        await btn.click()
        logger.info(f"[{label}] دکمه '{btn.text}' زده شد")
    else:
        # اگه روی همین پیام نبود منتظر می‌مونیم - چه با ادیت‌شدن همین پیام،
        # چه با یه پیام جدا؛ دقیقا الگوی کازینو: هم‌زمان به هر دو نوع event
        # گوش می‌دیم، مهم نیست بازی کدومش رو انتخاب می‌کنه
        try:
            btn_msg = await wait_step_any(
                client, group, lambda e: _has_button_containing(e, "یخچال"),
                "پیام یخچال (جدید یا ادیت‌شده) با دکمه یخچال", label, timeout=15,
            )
            btn = _find_button_containing(btn_msg, "یخچال")
            if btn:
                await btn.click()
                logger.info(f"[{label}] دکمه '{btn.text}' زده شد")
        except asyncio.TimeoutError:
            pass

    if not btn:
        logger.warning(f"[{label}] دکمه یخچال اصلا پیدا نشد، این دور رو رد می‌کنیم")
        return

    await asyncio.sleep(2)

    # ۳. باز کردن یخچال، زدن روی ماهی، زدن "بپزش"، تایید
    fridge_msg = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "یخچال میویی"),
        lambda e: e.buttons,
        "پیام یخچال با دکمه ماهی", label, timeout=20,
    )
    # آیتم‌های یخچال معمولا ایموجی سفارشی/پرمیوم دارن (متن دکمه خالیه)، پس اگه
    # با متن «🐟»/«🐠» پیدا نشد، اولین دکمه (چپ‌ترین، یعنی اولین آیتم لیست‌شده)
    # رو می‌زنیم - طبق ترتیب پیام، این همون ماهیه
    fish_btn = (
        _find_button_containing(fridge_msg, "🐟")
        or _find_button_containing(fridge_msg, "🐠")
        or _get_button_at(fridge_msg, 0, 0)
    )
    if not fish_btn:
        logger.warning(f"[{label}] دکمه ماهی توی یخچال پیدا نشد")
        return

    action_msg = await act_and_wait_any(
        client, group, fish_btn.click,
        lambda e: _has_button_containing(e, "پخیدن"),
        "پیام منوی ماهی با دکمه پخیدن", label, timeout=15,
    )
    pokh_btn = _find_button_containing(action_msg, "پخیدن")

    confirm_msg = await act_and_wait_any(
        client, group, pokh_btn.click,
        lambda e: _has_confirm_button(e),
        "پیام تایید پخت (✅)", label, timeout=15,
    )
    await _find_confirm_button(confirm_msg).click()
    logger.info(f"[{label}] پخت ماهی تایید شد، {COOK_WAIT // 60} دقیقه صبر می‌کنیم")

    # ۴. سی دقیقه صبر، بعد دوباره یخچال رو باز کن و این‌بار بفروش
    await asyncio.sleep(COOK_WAIT)

    fridge_msg2 = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "یخچال میویی"),
        lambda e: e.buttons,
        "پیام یخچال با دکمه ماهی برای فروش", label, timeout=20,
    )
    fish_btn2 = (
        _find_button_containing(fridge_msg2, "🐟")
        or _find_button_containing(fridge_msg2, "🐠")
        or _get_button_at(fridge_msg2, 0, 0)
    )
    if not fish_btn2:
        logger.warning(f"[{label}] دکمه ماهی برای فروش پیدا نشد")
        return

    sell_msg = await act_and_wait_any(
        client, group, fish_btn2.click,
        lambda e: _has_button_containing(e, "فروش"),
        "پیام منوی ماهی با دکمه فروش", label, timeout=15,
    )
    await _find_button_containing(sell_msg, "فروش").click()
    logger.info(f"[{label}] ماهی پخته‌شده فروخته شد ✅")


# ---------------------------------------------------------------------------
# روتین مستقل قاچاق میویی
# ---------------------------------------------------------------------------
SMUGGLE_WAIT = 70 * 60     # ۷۰ دقیقه صبر تا قاچاق تموم بشه
SMUGGLE_COOLDOWN = 5 * 60  # ۵ دقیقه صبر بعد از دریافت دستمزد قبل از دور بعدی
smuggling_tasks = {}       # label -> asyncio.Task


def start_smuggling(label, client):
    stop_smuggling(label)
    smuggling_tasks[label] = asyncio.create_task(smuggling_loop(label, client))
    logger.info(f"[{label}] روتین قاچاق میویی فعال شد")


def stop_smuggling(label):
    task = smuggling_tasks.pop(label, None)
    if task:
        task.cancel()


async def smuggling_loop(label, client):
    while True:
        try:
            await run_smuggling_cycle(label, client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا توی چرخه قاچاق: {type(e).__name__}: {e}")
            await asyncio.sleep(60)  # جلوگیری از لوپ سریع در صورت خطای مکرر


async def run_smuggling_cycle(label, client):
    group = _resolve_group(settings["group"])
    if not group:
        return

    # ۱. تا وقتی به صفحه "شروع قاچاق" برسیم ادامه بده - ممکنه بین راه
    # دستمزد قبلی نگرفته باشیم یا تو زندان باشیم، هر دو رو رد می‌کنیم
    start_btn = None
    for i in range(6):
        msg = await act_and_wait_any(
            client, group,
            lambda: client.send_message(group, "قاچاق میویی"),
            lambda e: (
                (_has_button_containing(e, "شروع") and _has_button_containing(e, "قاچاق"))
                or _has_button_containing(e, "دستمزد")
                or _has_button_containing(e, "زندان")
                or _has_button_containing(e, "آزاد")
                or _has_confirm_button(e)
            ),
            f"پاسخ به قاچاق میویی (شروع/دستمزد/زندان/انتخاب تعداد پیشی) (تلاش {i+1}/6)", label, timeout=25,
        )

        # صفحه «شروع قاچاق پیشی» رو باید قبل از پنل انتخاب تعداد چک کنیم چون هر دو
        # ممکنه توی متن‌شون "قاچاق پیشی" داشته باشن؛ فرقشون تو دکمه‌هاست: این یکی
        # دکمه «شروع» داره و ✅ نداره، اون یکی برعکس - ✅ داره و شروع نداره
        if _has_button_containing(msg, "شروع") and _has_button_containing(msg, "قاچاق"):
            start_btn = _find_button_containing(msg, "شروع")
            break

        if _has_button_containing(msg, "دستمزد"):
            # دستمزد دور قبل هنوز نگرفته بودیم؛ اول همون رو بگیر
            await _find_button_containing(msg, "دستمزد").click()
            logger.info(f"[{label}] دستمزد باقیمونده از دور قبل دریافت شد")
            await asyncio.sleep(2)
            continue

        if _has_button_containing(msg, "زندان") or _has_button_containing(msg, "آزاد"):
            # گیر افتادیم - باید جریمه رو بدیم و آزاد بشیم
            logger.info(f"[{label}] تو زندانیم، در حال پرداخت جریمه...")
            jail_msg = await act_and_wait_any(
                client, group,
                lambda: client.send_message(group, "زندان میویی"),
                lambda e: _has_button_containing(e, "جریمه"),
                "پیام زندان با دکمه جریمه", label, timeout=20,
            )
            jail_btn = _find_button_containing(jail_msg, "جریمه")

            confirm_jail = await act_and_wait_any(
                client, group, jail_btn.click,
                lambda e: _has_confirm_button(e),
                "تایید پرداخت جریمه (✅)", label, timeout=15,
            )
            await _find_confirm_button(confirm_jail).click()
            logger.info(f"[{label}] جریمه پرداخت شد، آزاد شدیم")
            await asyncio.sleep(3)
            continue

        if _has_confirm_button(msg):
            # صفحه انتخاب تعداد پیشی برای قاچاق (➖ ➕ ➕ / ✅ / BK) - دکمه ✅ این
            # یکی معمولا ایموجی سفارشی/پرمیومه و متنش خالیه، پس اگه با متن پیدا
            # نشد بر اساس موقعیت کلیک می‌کنیم تا تعداد پیش‌فرض تایید بشه و به
            # صفحه شروع قاچاق برسیم
            await _find_confirm_button(msg).click()
            logger.info(f"[{label}] پنل انتخاب تعداد پیشی قاچاقی: تعداد پیش‌فرض تایید شد")
            await asyncio.sleep(2)
            continue

        logger.warning(f"[{label}] پیام غیرمنتظره توی چرخه قاچاق، دوباره تلاش می‌کنیم")

    if not start_btn:
        logger.warning(f"[{label}] بعد از چند تلاش به صفحه شروع قاچاق نرسیدیم")
        return

    # ۲. تایید (دنبال دکمه‌ای با ایموجی ✅ بگرد - ممکنه ایموجی سفارشی با متن
    # خالی باشه، پس با هلپر _find_confirm_button که فال‌بک موقعیتی هم داره)
    confirm_msg = await act_and_wait_any(
        client, group, start_btn.click,
        lambda e: _has_confirm_button(e),
        "تایید شروع قاچاق (✅)", label, timeout=15,
    )
    await _find_confirm_button(confirm_msg).click()
    logger.info(f"[{label}] قاچاق تایید و شروع شد، {SMUGGLE_WAIT // 60} دقیقه صبر می‌کنیم")

    # ۳. صبر تا قاچاق تموم بشه
    await asyncio.sleep(SMUGGLE_WAIT)

    # ۴. دوباره بفرست تا نتیجه و دکمه دریافت دستمزد بیاد (یا زندان، اگه این بار گیر افتادیم)
    result_msg = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "قاچاق میویی"),
        lambda e: _has_button_containing(e, "دستمزد")
                  or _has_button_containing(e, "زندان")
                  or _has_button_containing(e, "آزاد"),
        "نتیجه قاچاق (دستمزد یا زندان)", label, timeout=30,
    )

    if _has_button_containing(result_msg, "دستمزد"):
        await _find_button_containing(result_msg, "دستمزد").click()
        logger.info(f"[{label}] دستمزد قاچاق دریافت شد ✅")
    else:
        # این دور گیر افتادیم؛ جریمه رو بده تا آزاد بشیم - دور بعد از اول شروع میشه
        logger.info(f"[{label}] این دور گیر افتادیم، در حال پرداخت جریمه...")
        jail_msg = await act_and_wait_any(
            client, group,
            lambda: client.send_message(group, "زندان میویی"),
            lambda e: _has_button_containing(e, "جریمه"),
            "پیام زندان با دکمه جریمه (دور دوم)", label, timeout=20,
        )
        jail_btn2 = _find_button_containing(jail_msg, "جریمه")
        confirm_jail = await act_and_wait_any(
            client, group, jail_btn2.click,
            lambda e: _has_confirm_button(e),
            "تایید پرداخت جریمه (دور دوم)", label, timeout=15,
        )
        await _find_confirm_button(confirm_jail).click()
        logger.info(f"[{label}] جریمه پرداخت شد، آزاد شدیم")

    # ۵. صبر قبل از شروع دور بعدی
    await asyncio.sleep(SMUGGLE_COOLDOWN)


# ---------------------------------------------------------------------------
# روتین تاس Saved Messages (تا وقتی ۸ تاس پشت‌سرهم زوج یا فرد نشه ادامه میده)
# ---------------------------------------------------------------------------
DICE_TARGET_STREAK_DEFAULT = 8
DICE_THROW_DELAY = 4.5   # فاصله بین تاس‌ها (ثانیه) - هم‌قد انیمیشن تاس تلگرام
dice_tasks = {}           # label -> asyncio.Task
bot_app = None            # بعد از ساخته‌شدن Application توی main() ست میشه، برای
                           # اینکه بشه از یه تسک پس‌زمینه (بدون update) به ادمین پیام داد


def _is_even(value: int) -> bool:
    return value % 2 == 0


def stop_dice_streak(label):
    task = dice_tasks.pop(label, None)
    if task:
        task.cancel()


async def run_dice_streak(label, client, target_streak, delay):
    streak_values = []
    last_is_even = None
    total_thrown = 0

    while True:
        message = await client.send_file("me", InputMediaDice(emoticon="🎲"))
        total_thrown += 1
        value = message.media.value
        current_is_even = _is_even(value)

        if last_is_even is None or current_is_even == last_is_even:
            streak_values.append(value)
        else:
            streak_values = [value]
        last_is_even = current_is_even

        parity_fa = "زوج" if current_is_even else "فرد"
        logger.info(
            f"[{label}] تاس #{total_thrown} -> {value} ({parity_fa}) | "
            f"رشته فعلی: {len(streak_values)}/{target_streak}"
        )

        if len(streak_values) >= target_streak:
            result_parity = "زوج" if last_is_even else "فرد"
            logger.info(f"[{label}] ✅ به {target_streak} تاس پشت‌سرهم {result_parity} رسید")
            if bot_app:
                try:
                    await bot_app.bot.send_message(
                        ADMIN_ID,
                        f"🎲 [{label}] به {target_streak} تاس پشت‌سرهم {result_parity} رسید!\n"
                        f"مقادیر: {streak_values}\n"
                        f"مجموع تاس‌های پرتاب‌شده: {total_thrown}",
                    )
                except Exception as e:
                    logger.warning(f"[{label}] خطا در اطلاع‌رسانی نتیجه تاس: {e}")
            return streak_values

        await asyncio.sleep(delay)


def start_dice_streak(label, client, target_streak=DICE_TARGET_STREAK_DEFAULT, delay=DICE_THROW_DELAY):
    stop_dice_streak(label)

    async def _runner():
        try:
            await run_dice_streak(label, client, target_streak, delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا توی روتین تاس: {type(e).__name__}: {e}")
            if bot_app:
                try:
                    await bot_app.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ [{label}] روتین تاس با خطا متوقف شد: {type(e).__name__}: {e}",
                    )
                except Exception:
                    pass
        finally:
            dice_tasks.pop(label, None)

    dice_tasks[label] = asyncio.create_task(_runner())


@admin_only
async def cmd_dicestreak(update, context):
    if not context.args:
        await update.message.reply_text(
            "استفاده: /dicestreak شماره_تلفن [تعداد_رشته]\n"
            "مثال: /dicestreak +989123456789 8"
        )
        return
    label = context.args[0]
    info = running.get(label)
    if not info:
        await update.message.reply_text("این اکانت در حال اجرا نیست. اول با /startaccount روشنش کن.")
        return
    if label in dice_tasks:
        await update.message.reply_text("این اکانت الان داره تاس می‌ندازه، صبر کن تموم بشه یا /stopdice بزن.")
        return

    target_streak = DICE_TARGET_STREAK_DEFAULT
    if len(context.args) > 1:
        try:
            target_streak = int(context.args[1])
            if target_streak < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text("تعداد رشته باید یه عدد صحیح مثبت باشه.")
            return

    start_dice_streak(label, info["client"], target_streak=target_streak)
    await update.message.reply_text(
        f"شروع شد: {label} داره تو Saved Messages تاس می‌ندازه تا "
        f"{target_streak} تا پشت‌سرهم زوج یا فرد بگیره. وقتی تموم شد بهت خبر میدم."
    )


@admin_only
async def cmd_stopdice(update, context):
    if not context.args:
        await update.message.reply_text("استفاده: /stopdice شماره_تلفن")
        return
    label = context.args[0]
    if label in dice_tasks:
        stop_dice_streak(label)
        await update.message.reply_text(f"روتین تاس {label} متوقف شد.")
    else:
        await update.message.reply_text("این اکانت الان تاس نمی‌اندازه.")


# ---------------------------------------------------------------------------
# روتین مستقل کازینو (هر ۶ دقیقه پیام "کازینو" ارسال می‌شه)
# ---------------------------------------------------------------------------
CASINO_INTERVAL = 6 * 60  # هر ۶ دقیقه
casino_tasks = {}         # label -> asyncio.Task


def start_casino(label, client):
    stop_casino(label)
    casino_tasks[label] = asyncio.create_task(casino_loop(label, client))
    logger.info(f"[{label}] روتین کازینو فعال شد")


def stop_casino(label):
    task = casino_tasks.pop(label, None)
    if task:
        task.cancel()


async def casino_loop(label, client):
    while True:
        start_t = asyncio.get_event_loop().time()
        try:
            await run_casino_cycle(label, client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا توی چرخه کازینو: {type(e).__name__}: {e}")
        elapsed = asyncio.get_event_loop().time() - start_t
        await asyncio.sleep(max(CASINO_INTERVAL - elapsed, 30))


CASINO_BET_AMOUNT = "20k"  # مبلغ پیش‌فرض ورودی گردونه شانس


async def run_casino_cycle(label, client):
    group = _resolve_group(settings["group"])
    if not group:
        return

    # ۱. ارسال «کازینو» و صبر برای منوی بازی‌ها (۳ دکمه ردیف اول + ۱ دکمه ردیف دوم)
    # نکته: دکمه‌های این منو ایموجی سفارشی دارن و متن‌شون خالیه (فقط کاراکتر
    # نامرئی)، پس با موقعیت شناسایی می‌کنیم نه با متن. ترتیب طبق چیدمان بازی:
    # ردیف ۰: [قمار میویی, گردونه شانس, تاس]  —  ردیف ۱: [معدن الماس]
    menu_msg = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "کازینو"),
        lambda e: e.buttons and len(e.buttons) == 2 and len(e.buttons[0]) == 3 and len(e.buttons[1]) == 1,
        "منوی کازینو (۳ دکمه ردیف اول + ۱ ردیف دوم)", label, timeout=30,
    )
    slot_btn = _get_button_at(menu_msg, 0, 2)
    if not slot_btn:
        logger.warning(f"[{label}] دکمه تاس (موقعیت ردیف ۰ ستون ۲) پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۲. کلیک روی تاس و صبر برای پنل تاس (دکمه تعیین مبلغ ورودی)
    panel_msg = await act_and_wait_any(
        client, group, slot_btn.click,
        lambda e: _has_button_containing(e, "تعیین مبلغ ورودی"),
        "پنل تاس (دکمه تعیین مبلغ ورودی)", label, timeout=20,
    )
    amount_btn = _find_button_containing(panel_msg, "تعیین مبلغ ورودی")
    if not amount_btn:
        logger.warning(f"[{label}] دکمه تعیین مبلغ ورودی پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۳. کلیک روی تعیین مبلغ ورودی و صبر برای پیام درخواست وارد کردن عدد
    prompt_msg = await act_and_wait_any(
        client, group, amount_btn.click,
        lambda e: e.raw_text and "وارد کنید" in _clean_text(e.raw_text),
        "درخواست وارد کردن مبلغ ورودی", label, timeout=15,
    )
    await asyncio.sleep(random.uniform(1, 2))

    # ۴. ریپلای با مبلغ (پیش‌فرض 10k) و صبر برای پنل به‌روزشده با دکمه تعیین تعداد بازیکن
    players_panel = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, CASINO_BET_AMOUNT, reply_to=prompt_msg.id),
        lambda e: _has_button_containing(e, "تعیین تعداد بازیکن"),
        "پنل به‌روزشده با دکمه تعیین تعداد بازیکن", label, timeout=15,
    )
    players_btn = _find_button_containing(players_panel, "تعیین تعداد بازیکن")
    if not players_btn:
        logger.warning(f"[{label}] دکمه تعیین تعداد بازیکن پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۵. کلیک روی تعیین تعداد بازیکن و صبر برای دکمه‌های عددی
    # این پیام ۲ ردیف داره: ردیف اول ۱ تا ۳ دکمه عددی + ردیف دوم ۱ دکمه (BK/کنسل).
    # تعداد دکمه‌های عددی ثابت نیست (بسته به مبلغ ورودی می‌تونه ۲ یا ۳ تا باشه)،
    # پس فقط شکل کلی (۲ ردیف، ردیف دوم = ۱ دکمه) و متن پیام رو چک می‌کنیم
    # («بازیکن» فقط توی پنل تعیین تعداد بازیکن هست، نه توی منوی اصلی).
    numbers_msg = await act_and_wait_any(
        client, group, players_btn.click,
        lambda e: (
            e.buttons and len(e.buttons) == 2
            and 1 <= len(e.buttons[0]) <= 3 and len(e.buttons[1]) == 1
            and e.raw_text and "بازیکن" in _clean_text(e.raw_text)
        ),
        "پیام انتخاب تعداد بازیکن (دکمه‌های عددی)", label, timeout=15,
    )
    one_btn = _get_button_at(numbers_msg, 0, 0)
    if not one_btn:
        logger.warning(f"[{label}] دکمه '1' (موقعیت ردیف ۰ ستون ۰) برای تعداد بازیکن پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۶. کلیک روی «1» و صبر برای پنل نهایی با دکمه ساخت میز قمار
    final_panel = await act_and_wait_any(
        client, group, one_btn.click,
        lambda e: _has_button_containing(e, "ساخت میز قمار"),
        "پنل نهایی با دکمه ساخت میز قمار", label, timeout=15,
    )
    build_btn = _find_button_containing(final_panel, "ساخت میز قمار")
    if not build_btn:
        logger.warning(f"[{label}] دکمه ساخت میز قمار پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۷. ریپلای واقعی (InputMediaDice) با 🎲 روی همون پنل نهایی، بعد ۵ ثانیه صبر
    await client.send_file(group, InputMediaDice(emoticon="🎲"), reply_to=final_panel.id)
    logger.info(f"[{label}] ایموجی 🎲 ۱/۲ (واقعی، قبل از ساخت میز) ارسال شد، ۵ ثانیه صبر می‌کنیم")
    await asyncio.sleep(5)

    # ۸. کلیک روی ساخت میز قمار
    await build_btn.click()
    logger.info(f"[{label}] دکمه 'ساخت میز قمار' زده شد")

    # ۹. دوباره ریپلای واقعی (InputMediaDice) با 🎲 روی همون پنل نهایی - پایان چرخه
    await client.send_file(group, InputMediaDice(emoticon="🎲"), reply_to=final_panel.id)
    logger.info(f"[{label}] ایموجی 🎲 ۲/۲ (واقعی، بعد از ساخت میز) ارسال شد ✅ چرخه کازینو تموم شد")

    # ۱۰. صبر برای پیام نتیجه (برد/باخت) و به‌روزرسانی آمار سود/ضرر کازینو
    # نمونه‌ی پیام نتیجه (فرض شده، مشابه گردونه شانس ولی با عنوان «تاس»):
    # 🃏 تاس 🔥😍
    # 💰 مبلغ ورودی : 20,000 🪙
    # 🏆 مبلغ دریافتی : 30,000 🪙 (1.5x)
    try:
        result_msg = await act_and_wait_any(
            client, group, lambda: asyncio.sleep(0),
            lambda e: (
                e.raw_text and "تاس" in _clean_text(e.raw_text)
                and "مبلغ دریافتی" in _clean_text(e.raw_text)
            ),
            "پیام نتیجه تاس (برد/باخت)", label, timeout=20,
        )
    except asyncio.TimeoutError:
        return

    text = _clean_text(result_msg.raw_text or "")
    entry_m = re.search(r"مبلغ ورودی\s*:\s*([\d,]+)", text)
    received_m = re.search(r"مبلغ دریافتی\s*:\s*([\d,]+)", text)
    if not (entry_m and received_m):
        logger.warning(f"[{label}] نتونستم مبلغ ورودی/دریافتی رو از پیام نتیجه پارس کنم: {text!r}")
        return

    entry_amt = int(entry_m.group(1).replace(",", ""))
    received_amt = int(received_m.group(1).replace(",", ""))
    round_profit = received_amt - entry_amt

    stats = settings.setdefault(
        "casino_stats", {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0}
    )
    stats["rounds"] += 1
    stats["total_in"] += entry_amt
    stats["total_out"] += received_amt
    if round_profit >= 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    data["settings"] = settings
    save_data()

    net_total = stats["total_out"] - stats["total_in"]
    logger.info(
        f"[{label}] نتیجه کازینو: ورودی={entry_amt:,} دریافتی={received_amt:,} "
        f"سود/ضرر این دور={round_profit:+,} | مجموع سود/ضرر تا الان={net_total:+,}"
    )


# ---------------------------------------------------------------------------
# روتین مستقل «کازینوی رشته‌ای»: تو Saved Messages انقدر تاس می‌ندازه تا ۸ تا
# پشت‌سرهم فرد یا زوج بیاد، بعد میره تو گروه و رو تاس، دقیقا مخالف اون رو
# انتخاب می‌کنه (اگه رشته فرد بود رو زوج شرط می‌بنده و برعکس)، میز رو می‌سازه
# و تاس واقعی می‌ندازه. بعد ۶ دقیقه صبر می‌کنه و دوباره از اول (شمارش تازه).
# ---------------------------------------------------------------------------
STREAK_BET_TARGET = 8              # چند تا پشت‌سرهم فرد/زوج لازمه
DICE_ANIMATION_SECONDS = 4.0       # مدت انیمیشن تاس تو کلاینت تلگرام (تقریبا ثابت)
STREAK_BET_COOLDOWN = 6 * 60       # فاصله بین هر دور کامل (شمارش + شرط) و دور بعدی
streak_casino_tasks = {}           # label -> asyncio.Task


def _load_streak_state(label):
    return settings.setdefault("streak_state", {}).get(
        label, {"streak_len": 0, "last_is_even": None, "total_thrown": 0}
    )


def _save_streak_state(label, state):
    settings.setdefault("streak_state", {})[label] = state
    data["settings"] = settings
    save_data()


def _clear_streak_state(label):
    settings.setdefault("streak_state", {}).pop(label, None)
    data["settings"] = settings
    save_data()


async def run_streak_dice_phase(label, client, target_streak=STREAK_BET_TARGET):
    """تو کانال شمارش (یا اگه تنظیم نشده بود، سیو مسیج) تاس واقعی می‌ندازه (و
    پیشرفت رو بعد از هر پرتاب سیو می‌کنه تا اگه ربات وسط کار ری‌استارت شد از
    همون‌جا ادامه بده) تا وقتی target_streak تا پشت‌سرهم فرد یا زوج بگیره.
    پاریتی مخالفِ رشته رو برمی‌گردونه (مثلا اگه ۸ تا فرد اومد، 'زوج' رو
    برمی‌گردونه تا رو گروه روش شرط ببندیم)."""
    dice_chat = settings.get("streak_dice_chat") or "me"
    dice_chat = _resolve_group(dice_chat) if dice_chat != "me" else "me"

    state = _load_streak_state(label)
    streak_len = state["streak_len"]
    last_is_even = state["last_is_even"]
    total_thrown = state["total_thrown"]

    while True:
        message = await client.send_file(dice_chat, InputMediaDice(emoticon="🎲"))
        total_thrown += 1
        value = message.media.value
        current_is_even = _is_even(value)

        if last_is_even is None or current_is_even == last_is_even:
            streak_len += 1
        else:
            streak_len = 1
        last_is_even = current_is_even

        parity_fa = "زوج" if current_is_even else "فرد"
        logger.info(
            f"[{label}] (کازینوی رشته‌ای) تاس #{total_thrown} -> {value} ({parity_fa}) | "
            f"رشته فعلی: {streak_len}/{target_streak}"
        )
        _save_streak_state(
            label, {"streak_len": streak_len, "last_is_even": last_is_even, "total_thrown": total_thrown}
        )

        if streak_len >= target_streak:
            result_parity = "زوج" if last_is_even else "فرد"
            opposite_parity = "فرد" if last_is_even else "زوج"
            logger.info(
                f"[{label}] ✅ (کازینوی رشته‌ای) به {target_streak} تا پشت‌سرهم {result_parity} رسید، "
                f"رو گروه '{opposite_parity}' شرط می‌بندیم"
            )
            _clear_streak_state(label)  # این رشته مصرف شد، شمارش بعدی از صفر شروع میشه

            # پیام تبریک تو همون چت شمارشی (کانال/سیو مسیج) + پین کردنش
            try:
                congrats_msg = await client.send_message(
                    dice_chat,
                    f"🎉 تبریک! {target_streak} تاس پشت‌سرهم {result_parity} اومد!\n"
                    f"داره میره تو گروه رو «{opposite_parity}» شرط می‌بنده.",
                )
                await client.pin_message(dice_chat, congrats_msg, notify=False)
                logger.info(f"[{label}] پیام تبریک فرستاده و پین شد")
            except Exception as e:
                logger.warning(f"[{label}] خطا در ارسال/پین پیام تبریک: {e}")

            if bot_app:
                try:
                    await bot_app.bot.send_message(
                        ADMIN_ID,
                        f"🎲 [{label}] به {target_streak} تاس پشت‌سرهم {result_parity} رسید! "
                        f"داره میره تو گروه رو '{opposite_parity}' شرط می‌بنده.",
                    )
                except Exception as e:
                    logger.warning(f"[{label}] خطا در اطلاع‌رسانی نتیجه تاس: {e}")
            return opposite_parity

        # صبر می‌کنیم انیمیشن تاس تو کلاینت تلگرام تموم بشه، بعد تاس بعدی رو می‌ندازیم
        await asyncio.sleep(DICE_ANIMATION_SECONDS)


async def run_streak_casino_cycle(label, client):
    group = _resolve_group(settings["group"])
    if not group:
        return

    # ۰. اول تو Saved Messages تاس می‌ندازیم تا رشته ۸تایی فرد/زوج شکل بگیره
    # و پاریتی مخالفش رو به‌دست بیاریم
    opposite_parity = await run_streak_dice_phase(label, client)

    # ۱. ارسال «کازینو» و صبر برای منوی بازی‌ها (همون منوی ۴ دکمه‌ای معمولی)
    menu_msg = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "کازینو"),
        lambda e: e.buttons and len(e.buttons) == 2 and len(e.buttons[0]) == 3 and len(e.buttons[1]) == 1,
        "منوی کازینو (۳ دکمه ردیف اول + ۱ ردیف دوم)", label, timeout=30,
    )
    slot_btn = _get_button_at(menu_msg, 0, 2)
    if not slot_btn:
        logger.warning(f"[{label}] (کازینوی رشته‌ای) دکمه تاس پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۲. کلیک روی تاس و صبر برای پنل تاس (دکمه تعیین مبلغ ورودی)
    panel_msg = await act_and_wait_any(
        client, group, slot_btn.click,
        lambda e: _has_button_containing(e, "تعیین مبلغ ورودی"),
        "پنل تاس (دکمه تعیین مبلغ ورودی)", label, timeout=20,
    )
    amount_btn = _find_button_containing(panel_msg, "تعیین مبلغ ورودی")
    if not amount_btn:
        logger.warning(f"[{label}] (کازینوی رشته‌ای) دکمه تعیین مبلغ ورودی پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۳. کلیک روی تعیین مبلغ ورودی و صبر برای پیام درخواست وارد کردن عدد
    prompt_msg = await act_and_wait_any(
        client, group, amount_btn.click,
        lambda e: e.raw_text and "وارد کنید" in _clean_text(e.raw_text),
        "درخواست وارد کردن مبلغ ورودی", label, timeout=15,
    )
    await asyncio.sleep(random.uniform(1, 2))

    # ۴. ریپلای با مبلغ و صبر برای پنل به‌روزشده با دکمه تعیین تعداد بازیکن
    players_panel = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, CASINO_BET_AMOUNT, reply_to=prompt_msg.id),
        lambda e: _has_button_containing(e, "تعیین تعداد بازیکن"),
        "پنل به‌روزشده با دکمه تعیین تعداد بازیکن", label, timeout=15,
    )
    players_btn = _find_button_containing(players_panel, "تعیین تعداد بازیکن")
    if not players_btn:
        logger.warning(f"[{label}] (کازینوی رشته‌ای) دکمه تعیین تعداد بازیکن پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۵. کلیک روی تعیین تعداد بازیکن و صبر برای دکمه‌های عددی
    # تعداد دکمه‌های عددی ثابت نیست (بسته به مبلغ ورودی می‌تونه ۲ یا ۳ تا باشه)
    numbers_msg = await act_and_wait_any(
        client, group, players_btn.click,
        lambda e: (
            e.buttons and len(e.buttons) == 2
            and 1 <= len(e.buttons[0]) <= 3 and len(e.buttons[1]) == 1
            and e.raw_text and "بازیکن" in _clean_text(e.raw_text)
        ),
        "پیام انتخاب تعداد بازیکن (دکمه‌های عددی)", label, timeout=15,
    )
    one_btn = _get_button_at(numbers_msg, 0, 0)
    if not one_btn:
        logger.warning(f"[{label}] (کازینوی رشته‌ای) دکمه '1' برای تعداد بازیکن پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۶. کلیک روی «1» و صبر برای پنل نهایی با دکمه ساخت میز قمار
    final_panel = await act_and_wait_any(
        client, group, one_btn.click,
        lambda e: _has_button_containing(e, "ساخت میز قمار"),
        "پنل نهایی با دکمه ساخت میز قمار", label, timeout=15,
    )
    build_btn = _find_button_containing(final_panel, "ساخت میز قمار")
    if not build_btn:
        logger.warning(f"[{label}] (کازینوی رشته‌ای) دکمه ساخت میز قمار پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۷. کلیک روی ساخت میز قمار (بدون ارسال تاس واقعی قبلش - تاس واقعی فقط
    # یه بار، در انتها و بعد از انتخاب مخالف پاریتی فرستاده می‌شه)
    await build_btn.click()
    logger.info(f"[{label}] (کازینوی رشته‌ای) دکمه 'ساخت میز قمار' زده شد")

    # ۸. بعد از ساخت میز، پنل انتخاب فرد/زوج (+ اعداد ۱ تا ۶) میاد - دقیقا
    # مخالف پاریتی رشته‌ای که تو Saved Messages به‌دست اومد رو کلیک می‌کنیم.
    # شکل این پنل: ردیف ۰ = ۲ دکمه (فرد/زوج)، ردیف ۱ و ۲ = هر کدوم ۳ دکمه (۱-۶)
    try:
        parity_panel = await act_and_wait_any(
            client, group, lambda: asyncio.sleep(0),
            lambda e: (
                e.buttons and len(e.buttons) == 3
                and len(e.buttons[0]) == 2 and len(e.buttons[1]) == 3 and len(e.buttons[2]) == 3
            ),
            "پنل انتخاب فرد/زوج بعد از ساخت میز", label, timeout=20,
        )
    except asyncio.TimeoutError:
        return

    parity_btn = _find_button_containing(parity_panel, opposite_parity)
    if not parity_btn:
        logger.warning(f"[{label}] (کازینوی رشته‌ای) دکمه '{opposite_parity}' پیدا نشد")
        return
    await parity_btn.click()
    logger.info(f"[{label}] (کازینوی رشته‌ای) دکمه '{opposite_parity}' زده شد")

    # ۹. ریپلای واقعی (InputMediaDice) با 🎲 روی پنل نهایی - تنها تاس واقعی این
    # چرخه، فرستاده‌شده بعد از انتخاب مخالف پاریتی - پایان چرخه
    await client.send_file(group, InputMediaDice(emoticon="🎲"), reply_to=final_panel.id)
    logger.info(f"[{label}] (کازینوی رشته‌ای) ایموجی 🎲 ارسال شد ✅ چرخه تموم شد")

    # ۱۰. صبر برای پیام نتیجه (برد/باخت) و به‌روزرسانی آمار سود/ضرر
    try:
        result_msg = await act_and_wait_any(
            client, group, lambda: asyncio.sleep(0),
            lambda e: (
                e.raw_text and "تاس" in _clean_text(e.raw_text)
                and "مبلغ دریافتی" in _clean_text(e.raw_text)
            ),
            "پیام نتیجه تاس (برد/باخت)", label, timeout=20,
        )
    except asyncio.TimeoutError:
        return

    text = _clean_text(result_msg.raw_text or "")
    entry_m = re.search(r"مبلغ ورودی\s*:\s*([\d,]+)", text)
    received_m = re.search(r"مبلغ دریافتی\s*:\s*([\d,]+)", text)
    if not (entry_m and received_m):
        logger.warning(f"[{label}] (کازینوی رشته‌ای) نتونستم مبلغ ورودی/دریافتی رو پارس کنم: {text!r}")
        return

    entry_amt = int(entry_m.group(1).replace(",", ""))
    received_amt = int(received_m.group(1).replace(",", ""))
    round_profit = received_amt - entry_amt

    stats = settings.setdefault(
        "streak_casino_stats", {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0}
    )
    stats["rounds"] += 1
    stats["total_in"] += entry_amt
    stats["total_out"] += received_amt
    if round_profit >= 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1
    data["settings"] = settings
    save_data()

    net_total = stats["total_out"] - stats["total_in"]
    logger.info(
        f"[{label}] (کازینوی رشته‌ای) نتیجه: ورودی={entry_amt:,} دریافتی={received_amt:,} "
        f"سود/ضرر این دور={round_profit:+,} | مجموع سود/ضرر تا الان={net_total:+,}"
    )


def start_streak_casino(label, client):
    stop_streak_casino(label)
    streak_casino_tasks[label] = asyncio.create_task(streak_casino_loop(label, client))
    logger.info(f"[{label}] روتین کازینوی رشته‌ای فعال شد")


def stop_streak_casino(label):
    task = streak_casino_tasks.pop(label, None)
    if task:
        task.cancel()


async def streak_casino_loop(label, client):
    while True:
        try:
            await run_streak_casino_cycle(label, client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا توی چرخه کازینوی رشته‌ای: {type(e).__name__}: {e}")
        await asyncio.sleep(STREAK_BET_COOLDOWN)


def apply_streak_casino_account():
    # کاملا مستقل از بقیه روتین‌ها - فقط یه اکانت باید کازینوی رشته‌ای رو انجام بده
    for lbl in list(streak_casino_tasks.keys()):
        stop_streak_casino(lbl)
    label = settings.get("streak_casino_account")
    if not label:
        return
    info = running.get(label)
    if info:
        start_streak_casino(label, info["client"])


# ---------------------------------------------------------------------------
# روتین مستقل پیشی (هر ۶ ساعت پیام "پیشی" ارسال و میو پوینت‌ها برداشت می‌شه)
# ---------------------------------------------------------------------------
PISHI_INTERVAL = 6 * 60 * 60  # هر ۶ ساعت
pishi_tasks = {}               # label -> asyncio.Task


def start_pishi(label, client):
    stop_pishi(label)
    pishi_tasks[label] = asyncio.create_task(pishi_loop(label, client))
    logger.info(f"[{label}] روتین پیشی فعال شد")


def stop_pishi(label):
    task = pishi_tasks.pop(label, None)
    if task:
        task.cancel()


async def pishi_loop(label, client):
    while True:
        start_t = asyncio.get_event_loop().time()
        try:
            await run_pishi_cycle(label, client)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[{label}] خطا توی چرخه پیشی: {type(e).__name__}: {e}")
        elapsed = asyncio.get_event_loop().time() - start_t
        await asyncio.sleep(max(PISHI_INTERVAL - elapsed, 60))


async def run_pishi_cycle(label, client):
    group = _resolve_group(settings["group"])
    if not group:
        return

    # ۱. ارسال «پیشی» و صبر برای پیام وضعیت با دکمه «برداشت میو پوینت ها»
    status_msg = await act_and_wait_any(
        client, group,
        lambda: client.send_message(group, "پیشی"),
        lambda e: _has_button_containing(e, "برداشت") and _has_button_containing(e, "میو پوینت"),
        "پیام وضعیت پیشی با دکمه برداشت میو پوینت ها", label, timeout=20,
    )
    withdraw_btn = _find_button_containing(status_msg, "برداشت")
    if not withdraw_btn:
        logger.warning(f"[{label}] دکمه برداشت میو پوینت ها پیدا نشد")
        return

    await withdraw_btn.click()
    logger.info(f"[{label}] دکمه 'برداشت میو پوینت ها' زده شد ✅")


def apply_pishi_account():
    # کاملا مستقل از بقیه روتین‌ها - فقط یه اکانت باید پیشی رو انجام بده
    for lbl in list(pishi_tasks.keys()):
        stop_pishi(lbl)
    label = settings.get("pishi_account")
    if not label:
        return
    info = running.get(label)
    if info:
        start_pishi(label, info["client"])


def apply_casino_account():
    # کاملا مستقل از بقیه روتین‌ها - فقط یه اکانت باید کازینو رو بفرسته
    for lbl in list(casino_tasks.keys()):
        stop_casino(lbl)
    label = settings.get("casino_account")
    if not label:
        return
    info = running.get(label)
    if info:
        start_casino(label, info["client"])


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


def apply_fisher_account():
    # کاملا مستقل از نجات میو - فقط یه اکانت باید ماهیگیر باشه
    for lbl in list(fishing_tasks.keys()):
        stop_fishing(lbl)
    label = settings.get("fisher_account")
    if not label:
        return
    info = running.get(label)
    if info:
        start_fishing(label, info["client"])


def apply_smuggler_account():
    # کاملا مستقل از بقیه - فقط یه اکانت باید قاچاقچی باشه
    for lbl in list(smuggling_tasks.keys()):
        stop_smuggling(lbl)
    label = settings.get("smuggler_account")
    if not label:
        return
    info = running.get(label)
    if info:
        start_smuggling(label, info["client"])


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
    apply_fisher_account()
    apply_smuggler_account()
    apply_casino_account()
    apply_pishi_account()
    apply_streak_casino_account()


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
async def cmd_setfisher(update, context):
    if not context.args:
        current = settings.get("fisher_account") or "هیچکدوم"
        await update.message.reply_text(
            f"اکانت ماهیگیر فعلی: {current}\n"
            "استفاده: /setfisher شماره_تلفن   (برای غیرفعال کردن: /setfisher off)"
        )
        return

    label = context.args[0]
    if label.lower() == "off":
        settings["fisher_account"] = None
        data["settings"] = settings
        save_data()
        apply_fisher_account()
        await update.message.reply_text("روتین ماهیگیری/یخچال غیرفعال شد.")
        return

    if label not in data["accounts"]:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return

    settings["fisher_account"] = label
    data["settings"] = settings
    save_data()
    apply_fisher_account()

    if label in running:
        await update.message.reply_text(
            f"اکانت {label} بعنوان ماهیگیر تنظیم شد و همین الان فعاله."
        )
    else:
        await update.message.reply_text(
            f"اکانت {label} بعنوان ماهیگیر تنظیم شد، ولی چون در حال اجرا نیست "
            f"اول با /startaccount {label} روشنش کن."
        )


@admin_only
async def cmd_setsmuggler(update, context):
    if not context.args:
        current = settings.get("smuggler_account") or "هیچکدوم"
        await update.message.reply_text(
            f"اکانت قاچاقچی فعلی: {current}\n"
            "استفاده: /setsmuggler شماره_تلفن   (برای غیرفعال کردن: /setsmuggler off)"
        )
        return

    label = context.args[0]
    if label.lower() == "off":
        settings["smuggler_account"] = None
        data["settings"] = settings
        save_data()
        apply_smuggler_account()
        await update.message.reply_text("روتین قاچاق میویی غیرفعال شد.")
        return

    if label not in data["accounts"]:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return

    settings["smuggler_account"] = label
    data["settings"] = settings
    save_data()
    apply_smuggler_account()

    if label in running:
        await update.message.reply_text(
            f"اکانت {label} بعنوان قاچاقچی تنظیم شد و همین الان فعاله."
        )
    else:
        await update.message.reply_text(
            f"اکانت {label} بعنوان قاچاقچی تنظیم شد، ولی چون در حال اجرا نیست "
            f"اول با /startaccount {label} روشنش کن."
        )


@admin_only
async def cmd_setcasino(update, context):
    if not context.args:
        current = settings.get("casino_account") or "هیچکدوم"
        await update.message.reply_text(
            f"اکانت کازینو فعلی: {current}\n"
            "استفاده: /setcasino شماره_تلفن   (برای غیرفعال کردن: /setcasino off)\n"
            f"این اکانت هر {CASINO_INTERVAL // 60} دقیقه یک دور کامل گردونه شانس "
            f"(کازینو) رو با مبلغ ورودی {CASINO_BET_AMOUNT} انجام می‌ده."
        )
        return

    label = context.args[0]
    if label.lower() == "off":
        settings["casino_account"] = None
        data["settings"] = settings
        save_data()
        apply_casino_account()
        await update.message.reply_text("روتین کازینو غیرفعال شد.")
        return

    if label not in data["accounts"]:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return

    settings["casino_account"] = label
    data["settings"] = settings
    save_data()
    apply_casino_account()

    if label in running:
        await update.message.reply_text(
            f"اکانت {label} بعنوان کازینو تنظیم شد و همین الان فعاله "
            f"(هر {CASINO_INTERVAL // 60} دقیقه یک دور کامل گردونه شانس با ورودی {CASINO_BET_AMOUNT} انجام می‌ده)."
        )
    else:
        await update.message.reply_text(
            f"اکانت {label} بعنوان کازینو تنظیم شد، ولی چون در حال اجرا نیست "
            f"اول با /startaccount {label} روشنش کن."
        )


@admin_only
async def cmd_setpishi(update, context):
    if not context.args:
        current = settings.get("pishi_account") or "هیچکدوم"
        await update.message.reply_text(
            f"اکانت پیشی فعلی: {current}\n"
            "استفاده: /setpishi شماره_تلفن   (برای غیرفعال کردن: /setpishi off)\n"
            f"این اکانت هر {PISHI_INTERVAL // 3600} ساعت پیام «پیشی» رو می‌فرسته "
            "و روی دکمه «برداشت میو پوینت ها» کلیک می‌کنه."
        )
        return

    label = context.args[0]
    if label.lower() == "off":
        settings["pishi_account"] = None
        data["settings"] = settings
        save_data()
        apply_pishi_account()
        await update.message.reply_text("روتین پیشی غیرفعال شد.")
        return

    if label not in data["accounts"]:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return

    settings["pishi_account"] = label
    data["settings"] = settings
    save_data()
    apply_pishi_account()

    if label in running:
        await update.message.reply_text(
            f"اکانت {label} بعنوان پیشی تنظیم شد و همین الان فعاله "
            f"(هر {PISHI_INTERVAL // 3600} ساعت «پیشی» می‌فرسته و میو پوینت‌ها رو برداشت می‌کنه)."
        )
    else:
        await update.message.reply_text(
            f"اکانت {label} بعنوان پیشی تنظیم شد، ولی چون در حال اجرا نیست "
            f"اول با /startaccount {label} روشنش کن."
        )


@admin_only
async def cmd_casinostats(update, context):
    stats = settings.get(
        "casino_stats", {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0}
    )
    net = stats["total_out"] - stats["total_in"]
    label_result = "سود" if net >= 0 else "ضرر"
    if context.args and context.args[0].lower() == "reset":
        settings["casino_stats"] = {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0}
        data["settings"] = settings
        save_data()
        await update.message.reply_text("آمار کازینو صفر شد.")
        return
    await update.message.reply_text(
        "📊 آمار کازینو:\n"
        f"تعداد دور: {stats['rounds']}\n"
        f"برد: {stats['wins']} | باخت: {stats['losses']}\n"
        f"مجموع مبلغ ورودی: {stats['total_in']:,}\n"
        f"مجموع مبلغ دریافتی: {stats['total_out']:,}\n"
        f"{label_result} کل: {abs(net):,}\n\n"
        "برای صفر کردن آمار: /casinostats reset"
    )


@admin_only
async def cmd_setstreakchannel(update, context):
    if not context.args:
        current = settings.get("streak_dice_chat") or "سیو مسیج (پیش‌فرض)"
        await update.message.reply_text(
            f"کانال فعلی برای انداختن تاس شمارشی: {current}\n"
            "استفاده: /setstreakchannel @یوزرنیم یا آیدی کانال   "
            "(برای برگشت به سیو مسیج: /setstreakchannel off)\n"
            "اکانتی که برای کازینوی رشته‌ای انتخاب کردی باید تو این کانال ادمین باشه "
            "و اجازه ارسال پیام داشته باشه."
        )
        return

    arg = context.args[0]
    if arg.lower() == "off":
        settings["streak_dice_chat"] = None
        data["settings"] = settings
        save_data()
        await update.message.reply_text("برگشت به سیو مسیج برای انداختن تاس شمارشی.")
        return

    chat = int(arg) if arg.lstrip("-").isdigit() else arg
    settings["streak_dice_chat"] = chat
    data["settings"] = settings
    save_data()
    await update.message.reply_text(f"کانال تاس شمارشی تنظیم شد: {chat}")


@admin_only
async def cmd_setstreakcasino(update, context):
    if not context.args:
        current = settings.get("streak_casino_account") or "هیچکدوم"
        await update.message.reply_text(
            f"اکانت کازینوی رشته‌ای فعلی: {current}\n"
            "استفاده: /setstreakcasino شماره_تلفن   (برای غیرفعال کردن: /setstreakcasino off)\n"
            f"این اکانت تو Saved Messages انقدر تاس می‌ندازه تا {STREAK_BET_TARGET} تا پشت‌سرهم "
            "فرد یا زوج بیاد، بعد میره تو گروه و دقیقا مخالفش رو با تاس شرط می‌بنده "
            f"(ورودی {CASINO_BET_AMOUNT})، و {STREAK_BET_COOLDOWN // 60} دقیقه بعد از هر دور دوباره از اول شروع می‌کنه."
        )
        return

    label = context.args[0]
    if label.lower() == "off":
        settings["streak_casino_account"] = None
        data["settings"] = settings
        save_data()
        apply_streak_casino_account()
        await update.message.reply_text("روتین کازینوی رشته‌ای غیرفعال شد.")
        return

    if label not in data["accounts"]:
        await update.message.reply_text("همچین اکانتی وجود نداره.")
        return

    settings["streak_casino_account"] = label
    data["settings"] = settings
    save_data()
    apply_streak_casino_account()

    if label in running:
        await update.message.reply_text(
            f"اکانت {label} بعنوان کازینوی رشته‌ای تنظیم شد و همین الان فعاله."
        )
    else:
        await update.message.reply_text(
            f"اکانت {label} بعنوان کازینوی رشته‌ای تنظیم شد، ولی چون در حال اجرا نیست "
            f"اول با /startaccount {label} روشنش کن."
        )


@admin_only
async def cmd_streakcasinostats(update, context):
    stats = settings.get(
        "streak_casino_stats", {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0}
    )
    net = stats["total_out"] - stats["total_in"]
    label_result = "سود" if net >= 0 else "ضرر"
    if context.args and context.args[0].lower() == "reset":
        settings["streak_casino_stats"] = {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0}
        data["settings"] = settings
        save_data()
        await update.message.reply_text("آمار کازینوی رشته‌ای صفر شد.")
        return
    await update.message.reply_text(
        "📊 آمار کازینوی رشته‌ای:\n"
        f"تعداد دور: {stats['rounds']}\n"
        f"برد: {stats['wins']} | باخت: {stats['losses']}\n"
        f"مجموع مبلغ ورودی: {stats['total_in']:,}\n"
        f"مجموع مبلغ دریافتی: {stats['total_out']:,}\n"
        f"{label_result} کل: {abs(net):,}\n\n"
        "برای صفر کردن آمار: /streakcasinostats reset"
    )


@admin_only
async def cmd_list(update, context):
    if not data["accounts"]:
        await update.message.reply_text("هیچ اکانتی اضافه نشده.")
        return
    lines = []
    for label in data["accounts"]:
        status = "فعال" if label in running else "متوقف"
        tag = ""
        if label == settings.get("rescue_account"):
            tag += " 🐱 نجات‌دهنده"
        if label == settings.get("fisher_account"):
            tag += " 🎣 ماهیگیر"
        if label == settings.get("smuggler_account"):
            tag += " 🕵️ قاچاقچی"
        if label == settings.get("casino_account"):
            tag += " 🎰 کازینو"
        if label == settings.get("pishi_account"):
            tag += " 🐈 پیشی"
        if label == settings.get("streak_casino_account"):
            tag += " 🎲 کازینوی رشته‌ای"
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
        stop_fishing(label)
        stop_smuggling(label)
        stop_casino(label)
        stop_pishi(label)
        stop_dice_streak(label)
        stop_streak_casino(label)
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
        stop_fishing(label)
        stop_smuggling(label)
        stop_casino(label)
        stop_pishi(label)
        stop_dice_streak(label)
        stop_streak_casino(label)
        await info["client"].disconnect()
    data["accounts"].pop(label, None)
    if settings.get("rescue_account") == label:
        settings["rescue_account"] = None
        data["settings"] = settings
    if settings.get("fisher_account") == label:
        settings["fisher_account"] = None
        data["settings"] = settings
    if settings.get("smuggler_account") == label:
        settings["smuggler_account"] = None
        data["settings"] = settings
    if settings.get("casino_account") == label:
        settings["casino_account"] = None
        data["settings"] = settings
    if settings.get("pishi_account") == label:
        settings["pishi_account"] = None
        data["settings"] = settings
    if settings.get("streak_casino_account") == label:
        settings["streak_casino_account"] = None
        data["settings"] = settings
    settings.get("streak_state", {}).pop(label, None)
    save_data()
    await update.message.reply_text(f"{label} حذف شد.")


@admin_only
async def cmd_start(update, context):
    await update.message.reply_text(
        "👋 دستورات ربات\n\n"
        "📱 اکانت‌ها\n"
        "/addaccount — اضافه کردن اکانت با شماره\n"
        "/importsession شماره session — اضافه با سشن آماده\n"
        "/list — لیست اکانت‌ها\n"
        "/startaccount شماره — استارت\n"
        "/stopaccount شماره — استاپ\n"
        "/removeaccount شماره — حذف کامل\n\n"
        "⚙️ تنظیمات پایه\n"
        "/setgroup — تنظیم گروه مقصد\n"
        "/setinterval min max — فاصله زمانی پیام (ثانیه)\n"
        "/setmessage — متن پیام\n\n"
        "🤖 روتین‌های خودکار (هر کدوم: شماره یا off)\n"
        "/setrescuer — کلیک خودکار «نجات میو»\n"
        "/setfisher — ماهیگیری/یخچال\n"
        "/setsmuggler — قاچاق میویی\n"
        "/setpishi — پیشی (هر ۶ ساعت)\n\n"
        "🎰 کازینو\n"
        "/setcasino شماره|off — کازینوی معمولی (هر ۶ دقیقه)\n"
        "/casinostats [reset] — آمار سود/ضرر\n"
        "/setstreakcasino شماره|off — کازینوی رشته‌ای (تاس تا ۸تایی)\n"
        "/setstreakchannel @کانال|off — کانال تاس شمارشی\n"
        "/streakcasinostats [reset] — آمار کازینوی رشته‌ای\n"
        "/dicestreak شماره [تعداد] — شمارش تک‌باره تو سیو مسیج\n"
        "/stopdice شماره — توقف شمارش تاس"
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
    app.add_handler(CommandHandler("setfisher", cmd_setfisher))
    app.add_handler(CommandHandler("setsmuggler", cmd_setsmuggler))
    app.add_handler(CommandHandler("setcasino", cmd_setcasino))
    app.add_handler(CommandHandler("setpishi", cmd_setpishi))
    app.add_handler(CommandHandler("casinostats", cmd_casinostats))
    app.add_handler(CommandHandler("dicestreak", cmd_dicestreak))
    app.add_handler(CommandHandler("stopdice", cmd_stopdice))
    app.add_handler(CommandHandler("setstreakcasino", cmd_setstreakcasino))
    app.add_handler(CommandHandler("setstreakchannel", cmd_setstreakchannel))
    app.add_handler(CommandHandler("streakcasinostats", cmd_streakcasinostats))

    global bot_app
    bot_app = app

    app.run_polling()


if __name__ == "__main__":
    main()
