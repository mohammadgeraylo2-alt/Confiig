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
    "casino_stats": {"rounds": 0, "wins": 0, "losses": 0, "total_in": 0, "total_out": 0},
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
    caught = await act_and_wait(
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
        # شاید دکمه‌ها با ادیت شدن همین پیام اضافه بشن، نه از اول توش باشن
        try:
            edited_same = await wait_step(
                client, group,
                lambda e: e.id == caught.id and _has_button_containing(e, "یخچال"),
                "ادیت شدن پیام گرفتن ماهی با دکمه یخچال", label, timeout=8, edited=True,
            )
            btn = _find_button_containing(edited_same, "یخچال")
            if btn:
                await btn.click()
                logger.info(f"[{label}] دکمه '{btn.text}' زده شد (بعد از ادیت)")
        except asyncio.TimeoutError:
            pass

    if not btn:
        # اگه روی همین پیام نبود، تا ۳ پیام شیشه‌ای بعدی رو هم چک کن (fallback)
        for i in range(3):
            try:
                btn_msg = await wait_step(
                    client, group, lambda e: _has_button_containing(e, "یخچال"),
                    f"پیام شیشه‌ای جدا با دکمه یخچال (تلاش {i+1}/3)", label, timeout=8,
                )
            except asyncio.TimeoutError:
                break
            btn = _find_button_containing(btn_msg, "یخچال")
            if btn:
                await btn.click()
                logger.info(f"[{label}] دکمه '{btn.text}' زده شد")
                break

    if not btn:
        logger.warning(f"[{label}] دکمه یخچال اصلا پیدا نشد، این دور رو رد می‌کنیم")
        return

    await asyncio.sleep(2)

    # ۳. باز کردن یخچال، زدن روی ماهی، زدن "بپزش"، تایید
    fridge_msg = await act_and_wait(
        client, group,
        lambda: client.send_message(group, "یخچال میویی"),
        lambda e: _has_button_containing(e, "🐟") or _has_button_containing(e, "🐠"),
        "پیام یخچال با دکمه ماهی (🐟/🐠)", label, timeout=20,
    )
    fish_btn = _find_button_containing(fridge_msg, "🐟") or _find_button_containing(fridge_msg, "🐠")
    if not fish_btn:
        logger.warning(f"[{label}] دکمه ماهی توی یخچال پیدا نشد")
        return

    action_msg = await act_and_wait(
        client, group, fish_btn.click,
        lambda e: _has_button_containing(e, "پخیدن"),
        "پیام منوی ماهی با دکمه پخیدن", label, timeout=15,
    )
    pokh_btn = _find_button_containing(action_msg, "پخیدن")

    confirm_msg = await act_and_wait(
        client, group, pokh_btn.click,
        lambda e: _has_button_containing(e, "✅"),
        "پیام تایید پخت (✅)", label, timeout=15,
    )
    await _find_button_containing(confirm_msg, "✅").click()
    logger.info(f"[{label}] پخت ماهی تایید شد، {COOK_WAIT // 60} دقیقه صبر می‌کنیم")

    # ۴. سی دقیقه صبر، بعد دوباره یخچال رو باز کن و این‌بار بفروش
    await asyncio.sleep(COOK_WAIT)

    fridge_msg2 = await act_and_wait(
        client, group,
        lambda: client.send_message(group, "یخچال میویی"),
        lambda e: _has_button_containing(e, "🐟") or _has_button_containing(e, "🐠"),
        "پیام یخچال با دکمه ماهی برای فروش", label, timeout=20,
    )
    fish_btn2 = _find_button_containing(fridge_msg2, "🐟") or _find_button_containing(fridge_msg2, "🐠")
    if not fish_btn2:
        logger.warning(f"[{label}] دکمه ماهی برای فروش پیدا نشد")
        return

    sell_msg = await act_and_wait(
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
        await client.send_message(group, "قاچاق میویی")
        logger.info(f"[{label}] پیام 'قاچاق میویی' ارسال شد (تلاش {i+1}/6)")

        msg = await wait_step(
            client, group,
            lambda e: (
                (_has_button_containing(e, "شروع") and _has_button_containing(e, "قاچاق"))
                or _has_button_containing(e, "دستمزد")
                or _has_button_containing(e, "زندان")
                or _has_button_containing(e, "آزاد")
            ),
            "پاسخ به قاچاق میویی (شروع/دستمزد/زندان)", label, timeout=25,
        )

        if _has_button_containing(msg, "دستمزد"):
            # دستمزد دور قبل هنوز نگرفته بودیم؛ اول همون رو بگیر
            await _find_button_containing(msg, "دستمزد").click()
            logger.info(f"[{label}] دستمزد باقیمونده از دور قبل دریافت شد")
            await asyncio.sleep(2)
            continue

        if _has_button_containing(msg, "زندان") or _has_button_containing(msg, "آزاد"):
            # گیر افتادیم - باید جریمه رو بدیم و آزاد بشیم
            logger.info(f"[{label}] تو زندانیم، در حال پرداخت جریمه...")
            await client.send_message(group, "زندان میویی")
            jail_msg = await wait_step(
                client, group, lambda e: _has_button_containing(e, "جریمه"),
                "پیام زندان با دکمه جریمه", label, timeout=20,
            )
            await _find_button_containing(jail_msg, "جریمه").click()

            confirm_jail = await wait_step(
                client, group, lambda e: _has_button_containing(e, "✅"),
                "تایید پرداخت جریمه (✅)", label, timeout=15,
            )
            await _find_button_containing(confirm_jail, "✅").click()
            logger.info(f"[{label}] جریمه پرداخت شد، آزاد شدیم")
            await asyncio.sleep(3)
            continue

        # چیزی جز اینا نمونده یعنی صفحه "شروع قاچاق" اومده
        start_btn = _find_button_containing(msg, "شروع")
        break

    if not start_btn:
        logger.warning(f"[{label}] بعد از چند تلاش به صفحه شروع قاچاق نرسیدیم")
        return

    await start_btn.click()

    # ۲. تایید (دنبال دکمه‌ای با ایموجی ✅ بگرد، نه اولین دکمه)
    confirm_msg = await wait_step(
        client, group, lambda e: _has_button_containing(e, "✅"),
        "تایید شروع قاچاق (✅)", label, timeout=15,
    )
    await _find_button_containing(confirm_msg, "✅").click()
    logger.info(f"[{label}] قاچاق تایید و شروع شد، {SMUGGLE_WAIT // 60} دقیقه صبر می‌کنیم")

    # ۳. صبر تا قاچاق تموم بشه
    await asyncio.sleep(SMUGGLE_WAIT)

    # ۴. دوباره بفرست تا نتیجه و دکمه دریافت دستمزد بیاد (یا زندان، اگه این بار گیر افتادیم)
    await client.send_message(group, "قاچاق میویی")
    result_msg = await wait_step(
        client, group,
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
        await client.send_message(group, "زندان میویی")
        jail_msg = await wait_step(
            client, group, lambda e: _has_button_containing(e, "جریمه"),
            "پیام زندان با دکمه جریمه (دور دوم)", label, timeout=20,
        )
        await _find_button_containing(jail_msg, "جریمه").click()
        confirm_jail = await wait_step(
            client, group, lambda e: _has_button_containing(e, "✅"),
            "تایید پرداخت جریمه (دور دوم)", label, timeout=15,
        )
        await _find_button_containing(confirm_jail, "✅").click()
        logger.info(f"[{label}] جریمه پرداخت شد، آزاد شدیم")

    # ۵. صبر قبل از شروع دور بعدی
    await asyncio.sleep(SMUGGLE_COOLDOWN)


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
    slot_btn = _get_button_at(menu_msg, 0, 1)
    if not slot_btn:
        logger.warning(f"[{label}] دکمه گردونه شانس (موقعیت ردیف ۰ ستون ۱) پیدا نشد")
        return
    await asyncio.sleep(random.uniform(1, 2))

    # ۲. کلیک روی 🎰 و صبر برای پنل گردونه شانس (دکمه تعیین مبلغ ورودی)
    panel_msg = await act_and_wait_any(
        client, group, slot_btn.click,
        lambda e: _has_button_containing(e, "تعیین مبلغ ورودی"),
        "پنل گردونه شانس (دکمه تعیین مبلغ ورودی)", label, timeout=20,
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

    # ۵. کلیک روی تعیین تعداد بازیکن و صبر برای دکمه‌های عددی ۱/۲/۳
    # این پیام ۲ ردیف داره: ردیف اول ۳ دکمه [۱, ۲, ۳] + ردیف دوم ۱ دکمه (BK/کنسل).
    # این شکل دقیقاً مثل منوی اصلی کازینوعه، پس علاوه بر شکل، متن پیام رو هم چک
    # می‌کنیم («بازیکن» فقط توی پنل تعیین تعداد بازیکن هست، نه توی منوی اصلی).
    numbers_msg = await act_and_wait_any(
        client, group, players_btn.click,
        lambda e: (
            e.buttons and len(e.buttons) == 2
            and len(e.buttons[0]) == 3 and len(e.buttons[1]) == 1
            and e.raw_text and "بازیکن" in _clean_text(e.raw_text)
        ),
        "پیام انتخاب تعداد بازیکن (دکمه‌های 1/2/3)", label, timeout=15,
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

    # ۷. ریپلای با 🎰 روی همون پنل نهایی، بعد ۵ ثانیه صبر
    await client.send_message(group, "🎰", reply_to=final_panel.id)
    logger.info(f"[{label}] ایموجی 🎰 ۱/۳ (قبل از ساخت میز) ارسال شد، ۵ ثانیه صبر می‌کنیم")
    await asyncio.sleep(5)

    # ۸. کلیک روی ساخت میز قمار
    await build_btn.click()
    logger.info(f"[{label}] دکمه 'ساخت میز قمار' زده شد")

    # ۹. دوباره ریپلای با 🎰 روی همون پنل نهایی، بعد ۵ ثانیه صبر
    await client.send_message(group, "🎰", reply_to=final_panel.id)
    logger.info(f"[{label}] ایموجی 🎰 ۲/۳ (بعد از ساخت میز) ارسال شد، ۵ ثانیه صبر می‌کنیم")
    await asyncio.sleep(5)

    # ۱۰. سومین و آخرین ریپلای با 🎰 روی همون پنل نهایی - پایان چرخه
    # این یکی رو صریحاً به شکل InputMediaDice می‌فرستیم تا همیشه یه پیام دایس/چرخشی
    # واقعی باشه (نه فقط یه کاراکتر متنی که ممکنه به این شکل تشخیص داده نشه)
    await client.send_file(group, InputMediaDice(emoticon="🎰"), reply_to=final_panel.id)
    logger.info(f"[{label}] ایموجی 🎰 ۳/۳ (به‌صورت دایس واقعی) ارسال شد ✅ چرخه کازینو تموم شد")

    # ۱۱. صبر برای پیام نتیجه (برد/باخت) و به‌روزرسانی آمار سود/ضرر کازینو
    # نمونه‌ی پیام نتیجه:
    # 🃏 گردونه شانس 🎰
    # 💰 مبلغ ورودی : 20,000 🪙
    # 🏆 مبلغ دریافتی : 30,000 🪙 (1.5x)
    try:
        result_msg = await act_and_wait_any(
            client, group, lambda: asyncio.sleep(0),
            lambda e: (
                e.raw_text and "گردونه شانس" in _clean_text(e.raw_text)
                and "مبلغ دریافتی" in _clean_text(e.raw_text)
            ),
            "پیام نتیجه گردونه شانس (برد/باخت)", label, timeout=20,
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
        "/setfisher شماره - انتخاب اکانتی که روتین ماهیگیری/یخچال رو انجام بده (off برای غیرفعال کردن)\n"
        "/setsmuggler شماره - انتخاب اکانتی که روتین قاچاق میویی رو انجام بده (off برای غیرفعال کردن)\n"
        "/setcasino شماره - انتخاب اکانتی که هر ۶ دقیقه یک دور کامل گردونه شانس کازینو رو انجام بده (off برای غیرفعال کردن)\n"
        "/casinostats - نمایش آمار سود/ضرر کازینو (reset برای صفر کردن)\n"
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
    app.add_handler(CommandHandler("setfisher", cmd_setfisher))
    app.add_handler(CommandHandler("setsmuggler", cmd_setsmuggler))
    app.add_handler(CommandHandler("setcasino", cmd_setcasino))
    app.add_handler(CommandHandler("casinostats", cmd_casinostats))

    app.run_polling()


if __name__ == "__main__":
    main()
