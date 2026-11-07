import asyncio
import json
import logging
import random
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

# ======================= НАСТРОЙКИ =======================
SHOP_NAME = "Hells Seller"
BOT_TOKEN = "9111944442:AAEJU0fGd6QeJ56bArhOZjN1ztqAAlfBRgE"
TON_WALLET = "UQBQmJ2M5NxBzUxP5m7Iy5OwoNz7Z81BjpzYunIqoQX1Lx3V"
ADMIN_IDS = [5637504709, 8583737882]
SUPPORT_URL = "https://t.me/HellsSuppBot"

# Стартовые пакеты (потом меняются из админ-панели)
DEFAULT_PACKAGES = {500: "1.5", 1000: "2.9", 3000: "8.5", 5000: "14", 10000: "27", 25000: "65"}
# =========================================================

logging.basicConfig(level=logging.INFO)

# ======================= БАЗА ДАННЫХ =======================
db = sqlite3.connect("bot.db")
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, name TEXT, lang TEXT,
    banned INTEGER DEFAULT 0, joined TEXT);
CREATE TABLE IF NOT EXISTS orders(
    id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, amount TEXT,
    txid TEXT UNIQUE, status TEXT DEFAULT 'pending', created TEXT);
CREATE TABLE IF NOT EXISTS reviews(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT,
    rating INTEGER, text TEXT, approved INTEGER DEFAULT 0, created TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
)
db.commit()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def q(sql, args=()):
    return db.execute(sql, args).fetchall()


def q1(sql, args=()):
    return db.execute(sql, args).fetchone()


def ex(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get_setting(k, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (k,))
    return r["value"] if r else default


def set_setting(k, v):
    ex("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))


def del_setting(k):
    ex("DELETE FROM settings WHERE key=?", (k,))


def get_packages():
    raw = get_setting("packages")
    d = json.loads(raw) if raw else {str(k): v for k, v in DEFAULT_PACKAGES.items()}
    return {int(k): v for k, v in sorted(d.items(), key=lambda x: int(x[0]))}


def save_packages(d):
    set_setting("packages", json.dumps({str(k): v for k, v in d.items()}))


def parse_price(s):
    d = Decimal(s.replace(",", ".").strip())
    if d <= 0:
        raise InvalidOperation
    return format(d.normalize(), "f")


def get_lang(uid):
    r = q1("SELECT lang FROM users WHERE id=?", (uid,))
    return r["lang"] if r and r["lang"] in ("en", "ru") else "en"


# ======================= PREMIUM EMOJI =======================
# ключ: обычный эмодзи (fallback). ID премиум-эмодзи задаются в админ-панели.
EMOJI = {
    "sparkle": "✨", "star": "⭐", "wave": "👋", "money": "💰", "wallet": "📥",
    "receipt": "🧾", "card": "💳", "note": "📝", "ok": "✅", "link": "🔗",
    "party": "🎉", "cross": "❌", "box": "📦", "faq": "❓", "review": "💬",
    "globe": "🌐", "diamond": "💎", "back": "⬅️", "pen": "✍️", "support": "🎧",
}
EMOJI_IDS = {}
ICONS_OFF = False  # включается автоматически, если Telegram не принимает иконки в кнопках


def load_emoji():
    for k in EMOJI:
        v = get_setting("emoji_" + k)
        if v:
            EMOJI_IDS[k] = v


def e(k):
    eid = EMOJI_IDS.get(k)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{EMOJI[k]}</tg-emoji>'
    return EMOJI[k]


# ======================= ТЕКСТЫ (i18n) =======================
TEXTS = {
    "en": {
        "welcome": "{sparkle} <b>{shop}</b>\nTelegram Stars for TON.\n\nHi, <b>{name}</b> {wave}",
        "select_pkg": "{star} <b>Select your package</b>\n\nAll prices are in TON.",
        "summary": "{receipt} <b>Order summary</b>\n\n{star} Stars: <b>{stars:,}</b>\n{money} Price: <b>{amount} TON</b>",
        "payment": (
            "{card} <b>TON Payment</b>\n\n{star} Stars: <b>{stars:,}</b>\n"
            "{money} Send exactly: <b>{amount} TON</b>\n\n{wallet} Wallet:\n<code>{wallet_addr}</code>\n\n"
            "Send the exact amount, then confirm below."
        ),
        "confirm_prompt": "{note} <b>Payment confirmation</b>\n\nPlease send your transaction hash (TXID) as plain text only.",
        "bad_txid": "⚠️ That doesn't look like a valid TXID. Send the hash as plain text.",
        "dup_txid": "⚠️ This transaction hash was already submitted.",
        "expired": "⚠️ Session expired. Please start again with /start.",
        "received": (
            "{ok} <b>Order received</b>\n\n{link} Order: <code>{oid}</code>\n{star} Stars: <b>{stars:,}</b>\n"
            "{money} Amount: <b>{amount} TON</b>\n\nWe are verifying your payment.\nYou will be notified after review."
        ),
        "completed": (
            "{party} <b>Order completed!</b>\n\n{link} Order: <code>{oid}</code>\n{star} Stars: <b>{stars:,}</b>\n\n"
            "Your payment has been confirmed and your Stars have been sent. Thank you for your purchase!"
        ),
        "rejected": (
            "{cross} <b>Payment not confirmed</b>\n\n{link} Order: <code>{oid}</code>\n\n"
            "We could not verify this payment. If you believe this is a mistake, contact support."
        ),
        "orders_title": "{box} <b>My orders</b>\n\n{lines}",
        "orders_empty": "You have no orders yet.",
        "st_pending": "⏳ Verifying", "st_completed": "✅ Completed", "st_rejected": "❌ Rejected",
        "faq_title": "{faq} <b>FAQ</b>\n\nChoose a question:",
        "reviews_title": "{review} <b>Reviews</b> ({total})\n\n{body}",
        "reviews_empty": "No reviews yet.",
        "rate_prompt": "Rate your experience:",
        "review_prompt": "Write your review (up to 500 characters):",
        "review_long": "⚠️ Too long. Maximum is 500 characters.",
        "review_thanks": "Thank you! Your review will appear after moderation.",
        "need_order": "Reviews are available after your first completed order.",
        "lang_title": "{globe} <b>Language</b>\n\nChoose your language:",
        "lang_set": "Language changed",
        "cancelled": "✖️ Cancelled.",
        "b_buy": "Buy Stars", "b_orders": "Orders", "b_reviews": "Reviews", "b_faq": "FAQ",
        "b_lang": "Language", "b_support": "Support", "b_menu": "Menu", "b_back": "Back",
        "b_pay": "Pay with TON", "b_paid": "I have paid", "b_cancel": "Cancel",
        "b_leave": "Leave a review",
    },
    "ru": {
        "welcome": "{sparkle} <b>{shop}</b>\nStars с откатом за TON!.\n\nПривет, <b>{name}</b> {wave}",
        "select_pkg": "{star} <b>Выберите пакет</b>\n\nВсе цены указаны в TON.",
        "summary": "{receipt} <b>Ваш заказ</b>\n\n{star} Stars: <b>{stars:,}</b>\n{money} Цена: <b>{amount} TON</b>",
        "payment": (
            "{card} <b>Оплата в TON</b>\n\n{star} Stars: <b>{stars:,}</b>\n"
            "{money} Отправьте ровно: <b>{amount} TON</b>\n\n{wallet} Кошелёк:\n<code>{wallet_addr}</code>\n\n"
            "Отправьте точную сумму, затем подтвердите ниже."
        ),
        "confirm_prompt": "{note} <b>Подтверждение оплаты</b>\n\nОтправьте хеш транзакции (TXID) только простым текстом.",
        "bad_txid": "⚠️ Это не похоже на TXID. Отправьте хеш простым текстом.",
        "dup_txid": "⚠️ Этот хеш транзакции уже был отправлен.",
        "expired": "⚠️ Сессия истекла. Начните заново: /start.",
        "received": (
            "{ok} <b>Заказ получен</b>\n\n{link} Заказ: <code>{oid}</code>\n{star} Stars: <b>{stars:,}</b>\n"
            "{money} Сумма: <b>{amount} TON</b>\n\nМы проверяем ваш платёж.\nВы получите уведомление после проверки."
        ),
        "completed": (
            "{party} <b>Заказ выполнен!</b>\n\n{link} Заказ: <code>{oid}</code>\n{star} Stars: <b>{stars:,}</b>\n\n"
            "Ваш платёж подтверждён, Stars отправлены. Спасибо за покупку!"
        ),
        "rejected": (
            "{cross} <b>Платёж не подтверждён</b>\n\n{link} Заказ: <code>{oid}</code>\n\n"
            "Мы не смогли подтвердить платёж. Если это ошибка — напишите в поддержку."
        ),
        "orders_title": "{box} <b>Мои заказы</b>\n\n{lines}",
        "orders_empty": "У вас пока нет заказов.",
        "st_pending": "⏳ Проверяется", "st_completed": "✅ Выполнен", "st_rejected": "❌ Отклонён",
        "faq_title": "{faq} <b>FAQ</b>\n\nВыберите вопрос:",
        "reviews_title": "{review} <b>Отзывы</b> ({total})\n\n{body}",
        "reviews_empty": "Отзывов пока нет.",
        "rate_prompt": "Оцените наш сервис:",
        "review_prompt": "Напишите отзыв (до 500 символов):",
        "review_long": "⚠️ Слишком длинно. Максимум 500 символов.",
        "review_thanks": "Спасибо! Ваш отзыв появится после модерации.",
        "need_order": "Оставить отзыв можно после первого выполненного заказа.",
        "lang_title": "{globe} <b>Язык</b>\n\nВыберите язык:",
        "lang_set": "Язык изменён",
        "cancelled": "✖️ Отменено.",
        "b_buy": "Купить Stars", "b_orders": "Заказы", "b_reviews": "Отзывы", "b_faq": "FAQ",
        "b_lang": "Язык", "b_support": "Поддержка", "b_menu": "Меню", "b_back": "Назад",
        "b_pay": "Оплатить TON", "b_paid": "Я оплатил", "b_cancel": "Отмена",
        "b_leave": "Оставить отзыв",
    },
}

# какой эмодзи стоит перед каждой пользовательской кнопкой
BTN_EMOJI = {
    "b_buy": "star", "b_orders": "box", "b_reviews": "review", "b_faq": "faq",
    "b_lang": "globe", "b_support": "support", "b_menu": "back", "b_back": "back",
    "b_pay": "diamond", "b_paid": "ok", "b_cancel": "back", "b_leave": "pen",
}

FAQ = {
    "en": [
        ("How long does delivery take?",
         "After you send the TXID, we verify the payment manually and send your Stars. You will get a notification as soon as the order is completed."),
        ("Where do I find the TXID?",
         "Open your TON wallet, find the outgoing transfer in the history and copy the transaction hash. Send it to the bot as plain text."),
        ("What if I sent the wrong amount?",
         "Contact support and give us your order number or TXID. We will help you sort it out."),
        ("Which wallets can I use?",
         "Any TON wallet that can send TON to the address shown in the bot."),
        ("Do you ask for my seed phrase?",
         "Never. We only need your transaction hash. Do not share your seed phrase or passwords with anyone."),
    ],
    "ru": [
        ("Как долго идёт выдача?",
         "После отправки TXID мы вручную проверяем платёж и отправляем Stars. Вы получите уведомление, как только заказ будет выполнен."),
        ("Где найти TXID?",
         "Откройте свой TON-кошелёк, найдите исходящий перевод в истории и скопируйте хеш транзакции. Отправьте его боту простым текстом."),
        ("Что если я отправил неверную сумму?",
         "Напишите в поддержку и укажите номер заказа или TXID. Мы поможем разобраться."),
        ("Какие кошельки подходят?",
         "Любой TON-кошелёк, из которого можно отправить TON на адрес, указанный в боте."),
        ("Вы просите seed-фразу?",
         "Никогда. Нам нужен только хеш транзакции. Не сообщайте seed-фразу и пароли никому."),
    ],
}

SECTIONS = {
    "menu": "Главное меню", "buy": "Выбор пакета", "summary": "Сводка заказа",
    "payment": "Оплата", "orders": "Мои заказы", "faq": "FAQ",
    "reviews": "Отзывы", "language": "Язык",
}


def tx(l, key, **kw):
    emo = {k: e(k) for k in EMOJI}
    return TEXTS[l][key].format(**emo, shop=SHOP_NAME, **kw)


# ======================= КНОПКИ =======================
def B(text, cb=None, url=None, ek=None):
    """ek — ключ эмодзи перед текстом. Если задан premium ID — используется иконка кнопки."""
    if ek:
        eid = None if ICONS_OFF else EMOJI_IDS.get(ek)
        if eid:
            return InlineKeyboardButton(text=text, callback_data=cb, url=url, icon_custom_emoji_id=eid)
        text = f"{EMOJI[ek]} {text}"
    return InlineKeyboardButton(text=text, callback_data=cb, url=url)


def UB(l, key, cb=None, url=None):
    return B(TEXTS[l][key], cb, url, BTN_EMOJI[key])


def M(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def has_icons(markup):
    return any(getattr(b, "icon_custom_emoji_id", None) for row in markup.inline_keyboard for b in row)


def strip_icons(markup):
    rev = {v: EMOJI[k] for k, v in EMOJI_IDS.items()}
    rows = []
    for row in markup.inline_keyboard:
        new = []
        for b in row:
            icon = getattr(b, "icon_custom_emoji_id", None)
            if icon:
                data = b.model_dump(exclude_none=True)
                data.pop("icon_custom_emoji_id", None)
                data["text"] = f"{rev.get(icon, '')} {b.text}".strip()
                b = InlineKeyboardButton(**data)
            new.append(b)
        rows.append(new)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SafeBot(Bot):
    """Если Telegram отклонил иконки в кнопках — повторяем запрос с обычными эмодзи."""

    async def __call__(self, method, request_timeout=None):
        global ICONS_OFF
        try:
            return await super().__call__(method, request_timeout=request_timeout)
        except TelegramBadRequest as err:
            markup = getattr(method, "reply_markup", None)
            if "not modified" in str(err) or not isinstance(markup, InlineKeyboardMarkup) or not has_icons(markup):
                raise
            method.reply_markup = strip_icons(markup)
            result = await super().__call__(method, request_timeout=request_timeout)
            ICONS_OFF = True
            logging.warning("Button icons rejected by Telegram, falling back to plain emoji: %s", err)
            return result


# ======================= ЭКРАНЫ (редактирование на месте) =======================
def parse_preview(v):
    if not v:
        return None
    if ":" in v:
        kind, fid = v.split(":", 1)
        return kind, fid
    return "photo", v


async def send_media(bot, chat_id, kind, fid, caption=None, markup=None):
    fn = {"photo": bot.send_photo, "video": bot.send_video, "animation": bot.send_animation}[kind]
    return await fn(chat_id, fid, caption=caption, reply_markup=markup)


def input_media(kind, fid, caption):
    cls = {"photo": InputMediaPhoto, "video": InputMediaVideo, "animation": InputMediaAnimation}[kind]
    return cls(media=fid, caption=caption)


async def deliver(bot, chat_id, pv, text, markup):
    if pv:
        try:
            if len(text) <= 1000:
                return await send_media(bot, chat_id, pv[0], pv[1], text, markup)
            await send_media(bot, chat_id, pv[0], pv[1])
        except TelegramBadRequest:
            pass
    return await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


async def show(target, section, text, markup=None):
    """Показывает экран. Для callback — редактирует сообщение, а не удаляет его."""
    pv = parse_preview(get_setting("preview_" + section))
    if not isinstance(target, CallbackQuery):
        return await deliver(target.bot, target.chat.id, pv, text, markup)

    msg = target.message
    has_media = bool(msg.photo or msg.video or msg.animation)
    try:
        if pv is None and not has_media:
            return await msg.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        if pv is not None and has_media and len(text) <= 1000:
            return await msg.edit_media(media=input_media(pv[0], pv[1], text), reply_markup=markup)
    except TelegramBadRequest as err:
        if "not modified" in str(err):
            return None
    # смена типа сообщения (текст <-> медиа): сначала отправляем новое, потом убираем старое
    new = await deliver(msg.bot, msg.chat.id, pv, text, markup)
    try:
        await msg.delete()
    except TelegramBadRequest:
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    return new


async def aedit(target, text, markup=None):
    """Админ-экран: редактирование/отправка."""
    if isinstance(target, CallbackQuery):
        try:
            return await target.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest:
            return None
    return await target.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ======================= MIDDLEWARE =======================
class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not user.is_bot:
            if not q1("SELECT id FROM users WHERE id=?", (user.id,)):
                lang = "ru" if (user.language_code or "").startswith("ru") else "en"
                ex("INSERT INTO users(id,username,name,lang,joined) VALUES(?,?,?,?,?)",
                   (user.id, user.username, user.full_name, lang, now()))
            else:
                ex("UPDATE users SET username=?, name=? WHERE id=?", (user.username, user.full_name, user.id))
            row = q1("SELECT banned FROM users WHERE id=?", (user.id,))
            if row["banned"] and user.id not in ADMIN_IDS:
                if getattr(event, "callback_query", None):
                    await event.callback_query.answer()
                return
        return await handler(event, data)


# ======================= FSM =======================
class Pay(StatesGroup):
    waiting_txid = State()


class Rev(StatesGroup):
    text = State()


class Adm(StatesGroup):
    find_user = State()
    write_user = State()
    bc_wait = State()
    price_edit = State()
    price_add = State()
    wait_preview = State()
    wait_emoji = State()
    rv_name = State()
    rv_rating = State()
    rv_text = State()


top = Router()
admin = Router()
user = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


# ======================= TOP: /start /cancel /admin =======================
def main_menu(l):
    return M(
        [UB(l, "b_buy", "buy")],
        [UB(l, "b_orders", "orders"), UB(l, "b_reviews", "reviews:0")],
        [UB(l, "b_faq", "faq"), UB(l, "b_lang", "lang")],
        [UB(l, "b_support", url=SUPPORT_URL)],
    )


async def send_menu(target, uid, name):
    l = get_lang(uid)
    await show(target, "menu", tx(l, "welcome", name=escape(name)), main_menu(l))


@top.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await send_menu(message, message.from_user.id, message.from_user.full_name)


@top.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(TEXTS[get_lang(message.from_user.id)]["cancelled"])


@top.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(admin_text(), reply_markup=admin_kb())


# ======================= ПОЛЬЗОВАТЕЛЬ =======================
@user.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_menu(call, call.from_user.id, call.from_user.full_name)
    await call.answer()


@user.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    items = [B(f"{s:,} — {p} TON", f"pkg:{s}", ek="star") for s, p in get_packages().items()]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    rows.append([UB(l, "b_menu", "menu")])
    await show(call, "buy", tx(l, "select_pkg"), M(*rows))
    await call.answer()


@user.callback_query(F.data.startswith("pkg:"))
async def cb_pkg(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    stars = int(call.data.split(":")[1])
    price = get_packages().get(stars)
    if not price:
        return await call.answer("Unavailable", show_alert=True)
    await show(call, "summary", tx(l, "summary", stars=stars, amount=price),
               M([UB(l, "b_pay", f"pay:{stars}")], [UB(l, "b_back", "buy")]))
    await call.answer()


@user.callback_query(F.data.startswith("pay:"))
async def cb_pay(call: CallbackQuery, state: FSMContext):
    l = get_lang(call.from_user.id)
    stars = int(call.data.split(":")[1])
    price = get_packages().get(stars)
    if not price:
        return await call.answer("Unavailable", show_alert=True)
    await state.update_data(stars=stars, amount=price)
    await show(call, "payment", tx(l, "payment", stars=stars, amount=price, wallet_addr=escape(TON_WALLET)),
               M([UB(l, "b_paid", "paid")], [UB(l, "b_cancel", "menu")]))
    await call.answer()


@user.callback_query(F.data == "paid")
async def cb_paid(call: CallbackQuery, state: FSMContext):
    l = get_lang(call.from_user.id)
    data = await state.get_data()
    if not data.get("stars"):
        await call.answer(TEXTS[l]["expired"], show_alert=True)
        return
    await state.set_state(Pay.waiting_txid)
    await call.message.answer(tx(l, "confirm_prompt"))
    await call.answer()


@user.message(Pay.waiting_txid, F.text)
async def got_txid(message: Message, state: FSMContext, bot: Bot):
    l = get_lang(message.from_user.id)
    txid = message.text.strip()
    if " " in txid or not (40 <= len(txid) <= 100):
        return await message.answer(TEXTS[l]["bad_txid"])
    data = await state.get_data()
    stars, amount = data.get("stars"), data.get("amount")
    if not stars:
        await state.clear()
        return await message.answer(TEXTS[l]["expired"])
    if q1("SELECT id FROM orders WHERE txid=?", (txid,)):
        return await message.answer(TEXTS[l]["dup_txid"])

    while True:
        oid = f"ORD-{random.randint(10000000, 99999999)}"
        if not q1("SELECT id FROM orders WHERE id=?", (oid,)):
            break
    ex("INSERT INTO orders(id,user_id,stars,amount,txid,created) VALUES(?,?,?,?,?,?)",
       (oid, message.from_user.id, stars, amount, txid, now()))
    await state.clear()
    await message.answer(tx(l, "received", oid=oid, stars=stars, amount=amount),
                         reply_markup=M([UB(l, "b_menu", "menu")]))

    for aid in ADMIN_IDS:
        try:
            await bot.send_message(
                aid, "🆕 <b>Новая заявка</b>\n\n" + order_card(q1("SELECT * FROM orders WHERE id=?", (oid,))),
                reply_markup=M([B("✅ Выполнить", f"a:ook:{oid}"), B("❌ Отклонить", f"a:ono:{oid}")]),
                disable_web_page_preview=True)
        except Exception:
            pass


@user.message(Pay.waiting_txid)
async def txid_not_text(message: Message):
    await message.answer(TEXTS[get_lang(message.from_user.id)]["bad_txid"])


@user.callback_query(F.data == "orders")
async def cb_orders(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    rows = q("SELECT * FROM orders WHERE user_id=? ORDER BY rowid DESC LIMIT 10", (call.from_user.id,))
    if rows:
        lines = "\n".join(
            f"{TEXTS[l]['st_' + r['status']]} · <code>{r['id']}</code>\n    ⭐ {r['stars']:,} · {r['amount']} TON · {r['created']}"
            for r in rows)
    else:
        lines = TEXTS[l]["orders_empty"]
    await show(call, "orders", tx(l, "orders_title", lines=lines), M([UB(l, "b_menu", "menu")]))
    await call.answer()


@user.callback_query(F.data == "faq")
async def cb_faq(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    rows = [[B(qn, f"faq:{i}")] for i, (qn, _) in enumerate(FAQ[l])]
    rows.append([UB(l, "b_menu", "menu")])
    await show(call, "faq", tx(l, "faq_title"), M(*rows))
    await call.answer()


@user.callback_query(F.data.startswith("faq:"))
async def cb_faq_item(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    qn, an = FAQ[l][int(call.data.split(":")[1])]
    await show(call, "faq", f"{e('faq')} <b>{escape(qn)}</b>\n\n{escape(an)}", M([UB(l, "b_back", "faq")]))
    await call.answer()


@user.callback_query(F.data.startswith("reviews:"))
async def cb_reviews(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    page = int(call.data.split(":")[1])
    per = 5
    total = q1("SELECT COUNT(*) c FROM reviews WHERE approved=1")["c"]
    rows = q("SELECT * FROM reviews WHERE approved=1 ORDER BY id DESC LIMIT ? OFFSET ?", (per, page * per))
    body = "\n\n".join(
        f"{'⭐' * r['rating']} <b>{escape(r['name'])}</b>\n{escape(r['text'])}" for r in rows
    ) or TEXTS[l]["reviews_empty"]
    nav = []
    if page > 0:
        nav.append(B("◀️", f"reviews:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(B("▶️", f"reviews:{page + 1}"))
    kb_rows = ([nav] if nav else []) + [[UB(l, "b_leave", "newrev")], [UB(l, "b_menu", "menu")]]
    await show(call, "reviews", tx(l, "reviews_title", total=total, body=body), M(*kb_rows))
    await call.answer()


@user.callback_query(F.data == "newrev")
async def cb_newrev(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    if not q1("SELECT id FROM orders WHERE user_id=? AND status='completed'", (call.from_user.id,)):
        return await call.answer(TEXTS[l]["need_order"], show_alert=True)
    await call.message.answer(TEXTS[l]["rate_prompt"],
                              reply_markup=M([B("⭐" * n, f"rate:{n}") for n in (1, 2, 3)],
                                             [B("⭐" * n, f"rate:{n}") for n in (4, 5)]))
    await call.answer()


@user.callback_query(F.data.startswith("rate:"))
async def cb_rate(call: CallbackQuery, state: FSMContext):
    l = get_lang(call.from_user.id)
    await state.set_state(Rev.text)
    await state.update_data(rating=int(call.data.split(":")[1]))
    await call.message.edit_text(TEXTS[l]["review_prompt"])
    await call.answer()


@user.message(Rev.text, F.text)
async def got_review(message: Message, state: FSMContext, bot: Bot):
    l = get_lang(message.from_user.id)
    if len(message.text) > 500:
        return await message.answer(TEXTS[l]["review_long"])
    data = await state.get_data()
    await state.clear()
    rid = ex("INSERT INTO reviews(user_id,name,rating,text,created) VALUES(?,?,?,?,?)",
             (message.from_user.id, message.from_user.first_name, data["rating"], message.text, now())).lastrowid
    await message.answer(TEXTS[l]["review_thanks"], reply_markup=M([UB(l, "b_menu", "menu")]))
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, "⭐ <b>Новый отзыв</b>\n\n" + review_card(q1("SELECT * FROM reviews WHERE id=?", (rid,))),
                                   reply_markup=M([B("✅ Опубликовать", f"a:rva:{rid}"), B("🗑 Удалить", f"a:rvd:{rid}")]))
        except Exception:
            pass


@user.callback_query(F.data == "lang")
async def cb_lang(call: CallbackQuery):
    l = get_lang(call.from_user.id)
    await show(call, "language", tx(l, "lang_title"),
               M([B("🇬🇧 English", "setlang:en"), B("🇷🇺 Русский", "setlang:ru")], [UB(l, "b_menu", "menu")]))
    await call.answer()


@user.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(call: CallbackQuery):
    lg = call.data.split(":")[1]
    if lg in ("en", "ru"):
        ex("UPDATE users SET lang=? WHERE id=?", (lg, call.from_user.id))
    await send_menu(call, call.from_user.id, call.from_user.full_name)
    await call.answer(TEXTS[get_lang(call.from_user.id)]["lang_set"])


# ======================= АДМИН: ВСПОМОГАТЕЛЬНОЕ =======================
def admin_text():
    users = q1("SELECT COUNT(*) c FROM users")["c"]
    banned = q1("SELECT COUNT(*) c FROM users WHERE banned=1")["c"]
    cnt = {r["status"]: r["c"] for r in q("SELECT status, COUNT(*) c FROM orders GROUP BY status")}
    revenue = sum((Decimal(r["amount"]) for r in q("SELECT amount FROM orders WHERE status='completed'")), Decimal(0))
    pend_rv = q1("SELECT COUNT(*) c FROM reviews WHERE approved=0")["c"]
    return (
        f"🛠 <b>Админ-панель · {escape(SHOP_NAME)}</b>\n\n"
        f"👥 Пользователей: <b>{users}</b> (забанено: {banned})\n"
        f"📦 Заказы: ⏳ {cnt.get('pending', 0)} · ✅ {cnt.get('completed', 0)} · ❌ {cnt.get('rejected', 0)}\n"
        f"💰 Выручка: <b>{format(revenue.normalize(), 'f')} TON</b>\n"
        f"⭐ Отзывов на модерации: <b>{pend_rv}</b>"
    )


def admin_kb():
    return M(
        [B("📦 Заказы", "a:orders"), B("👥 Пользователи", "a:users")],
        [B("⭐ Отзывы", "a:reviews"), B("📢 Рассылка", "a:bc")],
        [B("💰 Цены", "a:prices"), B("🖼 Превью", "a:prev")],
        [B("✨ Premium Emoji", "a:emoji")],
        [B("❌ Закрыть", "a:close")],
    )


def back(cb="a:menu", text="⬅️ Назад"):
    return M([B(text, cb)])


ST_ICON = {"pending": "⏳", "completed": "✅", "rejected": "❌"}


def order_card(o):
    u = q1("SELECT * FROM users WHERE id=?", (o["user_id"],))
    uname = f"@{u['username']}" if u and u["username"] else "—"
    name = escape(u["name"]) if u else "?"
    return (
        f"Заказ: <code>{o['id']}</code>\n"
        f"Статус: {ST_ICON[o['status']]} {o['status']}\n"
        f"Пользователь: <a href=\"tg://user?id={o['user_id']}\">{name}</a> ({uname}), ID <code>{o['user_id']}</code>\n"
        f"Звёзды: <b>{o['stars']:,}</b>\nСумма: <b>{o['amount']} TON</b>\n"
        f"TXID: <code>{escape(o['txid'])}</code>\n"
        f"https://tonviewer.com/transaction/{escape(o['txid'])}\n"
        f"Создан: {o['created']}"
    )


def review_card(r):
    return (
        f"Отзыв #{r['id']} · {'✅ опубликован' if r['approved'] else '⏳ на модерации'}\n"
        f"{'⭐' * r['rating']} <b>{escape(r['name'])}</b> (ID {r['user_id']})\n\n{escape(r['text'])}"
    )


def user_card(u):
    done = q("SELECT amount FROM orders WHERE user_id=? AND status='completed'", (u["id"],))
    spent = sum((Decimal(r["amount"]) for r in done), Decimal(0))
    return (
        f"👤 <a href=\"tg://user?id={u['id']}\">{escape(u['name'] or '?')}</a>\n"
        f"ID: <code>{u['id']}</code>\nUsername: {'@' + u['username'] if u['username'] else '—'}\n"
        f"Язык: {u['lang']}\nРегистрация: {u['joined']}\n"
        f"Выполненных заказов: <b>{len(done)}</b> на <b>{format(spent.normalize(), 'f')} TON</b>\n"
        f"Статус: {'🚫 забанен' if u['banned'] else '🟢 активен'}"
    )


# ======================= АДМИН: МЕНЮ =======================
@admin.callback_query(F.data == "a:menu")
async def a_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await aedit(call, admin_text(), admin_kb())
    await call.answer()


@admin.callback_query(F.data == "a:close")
async def a_close(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except TelegramBadRequest:
        pass
    await call.answer()


# ---------- Заказы ----------
@admin.callback_query(F.data == "a:orders")
async def a_orders(call: CallbackQuery):
    p = q1("SELECT COUNT(*) c FROM orders WHERE status='pending'")["c"]
    await aedit(call, "📦 <b>Заказы</b>",
                M([B(f"⏳ Ожидают ({p})", "a:ol:pending")], [B("📋 Последние", "a:ol:all")], [B("⬅️ Назад", "a:menu")]))
    await call.answer()


@admin.callback_query(F.data.startswith("a:ol:"))
async def a_order_list(call: CallbackQuery):
    f = call.data.split(":")[2]
    rows = q("SELECT * FROM orders WHERE status='pending' ORDER BY rowid DESC LIMIT 20") if f == "pending" \
        else q("SELECT * FROM orders ORDER BY rowid DESC LIMIT 20")
    kb_rows = [[B(f"{ST_ICON[r['status']]} {r['id']} · {r['stars']:,}⭐ · {r['amount']} TON", f"a:oc:{r['id']}")] for r in rows]
    kb_rows.append([B("⬅️ Назад", "a:orders")])
    await aedit(call, "📦 <b>Список заказов</b>" + ("" if rows else "\n\nПусто."), M(*kb_rows))
    await call.answer()


@admin.callback_query(F.data.startswith("a:oc:"))
async def a_order_card(call: CallbackQuery):
    o = q1("SELECT * FROM orders WHERE id=?", (call.data.split(":")[2],))
    if not o:
        return await call.answer("Не найден", show_alert=True)
    rows = []
    if o["status"] == "pending":
        rows.append([B("✅ Выполнить", f"a:ook:{o['id']}"), B("❌ Отклонить", f"a:ono:{o['id']}")])
    rows.append([B("⬅️ Назад", "a:ol:all")])
    await aedit(call, order_card(o), M(*rows))
    await call.answer()


@admin.callback_query(F.data.regexp(r"^a:o(ok|no):ORD-\d+$"))
async def a_order_process(call: CallbackQuery, bot: Bot):
    _, act, oid = call.data.split(":")
    o = q1("SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        return await call.answer("Не найден", show_alert=True)
    if o["status"] != "pending":
        return await call.answer(f"Уже обработан: {o['status']}", show_alert=True)
    l = get_lang(o["user_id"])
    if act == "ook":
        ex("UPDATE orders SET status='completed' WHERE id=?", (oid,))
        text = tx(l, "completed", oid=oid, stars=o["stars"])
        kb = M([UB(l, "b_reviews", "reviews:0")], [UB(l, "b_menu", "menu")])
        mark = "✅ Выполнено, пользователь уведомлён"
    else:
        ex("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
        text = tx(l, "rejected", oid=oid)
        kb = M([UB(l, "b_support", url=SUPPORT_URL)])
        mark = "❌ Отклонено, пользователь уведомлён"
    try:
        await bot.send_message(o["user_id"], text, reply_markup=kb)
    except Exception:
        mark += " (не удалось отправить сообщение)"
    await aedit(call, order_card(q1("SELECT * FROM orders WHERE id=?", (oid,))) + f"\n\n<b>{mark}</b>",
                back("a:orders"))
    await call.answer()


# ---------- Пользователи ----------
@admin.callback_query(F.data == "a:users")
async def a_users(call: CallbackQuery):
    await aedit(call, "👥 <b>Пользователи</b>",
                M([B("🔍 Найти (ID / @username)", "a:ufind")],
                  [B("🆕 Последние", "a:ul:new"), B("🚫 Забаненные", "a:ul:ban")],
                  [B("⬅️ Назад", "a:menu")]))
    await call.answer()


@admin.callback_query(F.data.startswith("a:ul:"))
async def a_user_list(call: CallbackQuery):
    f = call.data.split(":")[2]
    rows = q("SELECT * FROM users WHERE banned=1 ORDER BY rowid DESC LIMIT 20") if f == "ban" \
        else q("SELECT * FROM users ORDER BY rowid DESC LIMIT 20")
    kb_rows = [[B(f"{'🚫 ' if r['banned'] else ''}{(r['name'] or '?')[:25]} · {r['id']}", f"a:uc:{r['id']}")] for r in rows]
    kb_rows.append([B("⬅️ Назад", "a:users")])
    await aedit(call, "👥 <b>Список</b>" + ("" if rows else "\n\nПусто."), M(*kb_rows))
    await call.answer()


@admin.callback_query(F.data == "a:ufind")
async def a_ufind(call: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.find_user)
    await aedit(call, "Отправь ID или @username пользователя.\n/cancel — отмена", back("a:users"))
    await call.answer()


@admin.message(Adm.find_user, F.text)
async def a_ufind_got(message: Message, state: FSMContext):
    s = message.text.strip().lstrip("@")
    u = q1("SELECT * FROM users WHERE id=? OR lower(username)=?", (int(s) if s.isdigit() else -1, s.lower()))
    if not u:
        return await message.answer("Не найден. Попробуй ещё раз или /cancel")
    await state.clear()
    await message.answer(user_card(u), reply_markup=user_actions(u))


def user_actions(u):
    return M([B("✅ Разбанить" if u["banned"] else "🚫 Забанить", f"a:ub:{u['id']}"), B("✉️ Написать", f"a:uw:{u['id']}")],
             [B("⬅️ Назад", "a:users")])


@admin.callback_query(F.data.startswith("a:uc:"))
async def a_user_card(call: CallbackQuery):
    u = q1("SELECT * FROM users WHERE id=?", (int(call.data.split(":")[2]),))
    if not u:
        return await call.answer("Не найден", show_alert=True)
    await aedit(call, user_card(u), user_actions(u))
    await call.answer()


@admin.callback_query(F.data.startswith("a:ub:"))
async def a_user_ban(call: CallbackQuery):
    uid = int(call.data.split(":")[2])
    if uid in ADMIN_IDS:
        return await call.answer("Нельзя банить админа", show_alert=True)
    ex("UPDATE users SET banned = 1 - banned WHERE id=?", (uid,))
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    await aedit(call, user_card(u), user_actions(u))
    await call.answer("Готово")


@admin.callback_query(F.data.startswith("a:uw:"))
async def a_user_write(call: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.write_user)
    await state.update_data(uid=int(call.data.split(":")[2]))
    await aedit(call, "Отправь сообщение для пользователя.\n/cancel — отмена")
    await call.answer()


@admin.message(Adm.write_user)
async def a_user_write_got(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    try:
        await bot.copy_message(data["uid"], message.chat.id, message.message_id)
        await message.answer("✅ Отправлено", reply_markup=back("a:users"))
    except Exception as ex_:
        await message.answer(f"❌ Не удалось отправить: {ex_}", reply_markup=back("a:users"))


# ---------- Отзывы ----------
@admin.callback_query(F.data == "a:reviews")
async def a_reviews(call: CallbackQuery):
    p = q1("SELECT COUNT(*) c FROM reviews WHERE approved=0")["c"]
    await aedit(call, "⭐ <b>Отзывы</b>",
                M([B(f"⏳ На модерации ({p})", "a:rl:p")], [B("📋 Все", "a:rl:a")],
                  [B("➕ Добавить вручную", "a:radd")], [B("⬅️ Назад", "a:menu")]))
    await call.answer()


@admin.callback_query(F.data.startswith("a:rl:"))
async def a_review_list(call: CallbackQuery):
    f = call.data.split(":")[2]
    rows = q("SELECT * FROM reviews WHERE approved=0 ORDER BY id DESC LIMIT 20") if f == "p" \
        else q("SELECT * FROM reviews ORDER BY id DESC LIMIT 20")
    kb_rows = [[B(f"#{r['id']} {'⭐' * r['rating']} {r['name'][:20]} {'✅' if r['approved'] else '⏳'}", f"a:rvc:{r['id']}")] for r in rows]
    kb_rows.append([B("⬅️ Назад", "a:reviews")])
    await aedit(call, "⭐ <b>Список отзывов</b>" + ("" if rows else "\n\nПусто."), M(*kb_rows))
    await call.answer()


@admin.callback_query(F.data.startswith("a:rvc:"))
async def a_review_card(call: CallbackQuery):
    r = q1("SELECT * FROM reviews WHERE id=?", (int(call.data.split(":")[2]),))
    if not r:
        return await call.answer("Не найден", show_alert=True)
    row = []
    if not r["approved"]:
        row.append(B("✅ Опубликовать", f"a:rva:{r['id']}"))
    row.append(B("🗑 Удалить", f"a:rvd:{r['id']}"))
    await aedit(call, review_card(r), M(row, [B("⬅️ Назад", "a:rl:a")]))
    await call.answer()


@admin.callback_query(F.data.startswith("a:rva:"))
async def a_review_approve(call: CallbackQuery):
    ex("UPDATE reviews SET approved=1 WHERE id=?", (int(call.data.split(":")[2]),))
    await aedit(call, "✅ Отзыв опубликован", back("a:reviews"))
    await call.answer()


@admin.callback_query(F.data.startswith("a:rvd:"))
async def a_review_delete(call: CallbackQuery):
    ex("DELETE FROM reviews WHERE id=?", (int(call.data.split(":")[2]),))
    await aedit(call, "🗑 Отзыв удалён", back("a:reviews"))
    await call.answer()


@admin.callback_query(F.data == "a:radd")
async def a_review_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.rv_name)
    await aedit(call, "Имя автора отзыва:\n/cancel — отмена")
    await call.answer()


@admin.message(Adm.rv_name, F.text)
async def a_rv_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:40])
    await state.set_state(Adm.rv_rating)
    await message.answer("Оценка (1-5):")


@admin.message(Adm.rv_rating, F.text)
async def a_rv_rating(message: Message, state: FSMContext):
    if message.text.strip() not in ("1", "2", "3", "4", "5"):
        return await message.answer("Введи число от 1 до 5")
    await state.update_data(rating=int(message.text.strip()))
    await state.set_state(Adm.rv_text)
    await message.answer("Текст отзыва:")


@admin.message(Adm.rv_text, F.text)
async def a_rv_text(message: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    ex("INSERT INTO reviews(user_id,name,rating,text,approved,created) VALUES(0,?,?,?,1,?)",
       (d["name"], d["rating"], message.text[:500], now()))
    await message.answer("✅ Отзыв добавлен и опубликован", reply_markup=back("a:reviews"))


# ---------- Рассылка ----------
@admin.callback_query(F.data == "a:bc")
async def a_bc(call: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.bc_wait)
    await aedit(call, "📢 <b>Рассылка</b>\n\nОтправь сообщение (текст, фото, видео и т.д.) — оно будет разослано всем пользователям.\n/cancel — отмена",
                back())
    await call.answer()


@admin.message(Adm.bc_wait)
async def a_bc_got(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(chat=message.chat.id, mid=message.message_id)
    n = q1("SELECT COUNT(*) c FROM users WHERE banned=0")["c"]
    await message.answer("👆 Так увидят пользователи.")
    await message.answer(f"Разослать <b>{n}</b> пользователям?",
                         reply_markup=M([B("🚀 Отправить", "a:bcgo"), B("✖️ Отмена", "a:menu")]))


async def send_copy(bot, uid, chat, mid):
    for _ in range(2):
        try:
            await bot.copy_message(uid, chat, mid)
            return True
        except TelegramRetryAfter as ex_:
            await asyncio.sleep(ex_.retry_after + 1)
        except Exception:
            return False
    return False


@admin.callback_query(F.data == "a:bcgo")
async def a_bc_go(call: CallbackQuery, state: FSMContext, bot: Bot):
    d = await state.get_data()
    await state.clear()
    if not d.get("mid"):
        return await call.answer("Нет сообщения", show_alert=True)
    ids = [r["id"] for r in q("SELECT id FROM users WHERE banned=0")]
    await aedit(call, f"⏳ Рассылка запущена ({len(ids)})...")
    await call.answer()
    ok = fail = 0
    for uid in ids:
        if await send_copy(bot, uid, d["chat"], d["mid"]):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    await call.message.answer(f"✅ Рассылка завершена\n\nДоставлено: <b>{ok}</b>\nОшибок: <b>{fail}</b>", reply_markup=back())


# ---------- Цены ----------
@admin.callback_query(F.data == "a:prices")
async def a_prices(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[B(f"⭐ {s:,} — {p} TON", f"a:pc:{s}")] for s, p in get_packages().items()]
    rows += [[B("➕ Добавить пакет", "a:padd")], [B("⬅️ Назад", "a:menu")]]
    await aedit(call, "💰 <b>Цены</b>\n\nВыбери пакет:", M(*rows))
    await call.answer()


@admin.callback_query(F.data.startswith("a:pc:"))
async def a_price_card(call: CallbackQuery):
    s = int(call.data.split(":")[2])
    p = get_packages().get(s)
    if not p:
        return await call.answer("Не найден", show_alert=True)
    await aedit(call, f"⭐ <b>{s:,}</b> Stars — <b>{p} TON</b>",
                M([B("✏️ Изменить цену", f"a:pe:{s}"), B("🗑 Удалить", f"a:pd:{s}")], [B("⬅️ Назад", "a:prices")]))
    await call.answer()


@admin.callback_query(F.data.startswith("a:pe:"))
async def a_price_edit(call: CallbackQuery, state: FSMContext):
    s = int(call.data.split(":")[2])
    await state.set_state(Adm.price_edit)
    await state.update_data(stars=s)
    await aedit(call, f"Новая цена для {s:,} Stars в TON (например 1.5):\n/cancel — отмена")
    await call.answer()


@admin.message(Adm.price_edit, F.text)
async def a_price_edit_got(message: Message, state: FSMContext):
    try:
        price = parse_price(message.text)
    except (InvalidOperation, ValueError):
        return await message.answer("Неверное число. Пример: 1.5")
    d = await state.get_data()
    await state.clear()
    pk = get_packages()
    pk[d["stars"]] = price
    save_packages(pk)
    await message.answer(f"✅ {d['stars']:,} Stars — {price} TON", reply_markup=back("a:prices"))


@admin.callback_query(F.data == "a:padd")
async def a_price_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.price_add)
    await aedit(call, "Отправь «количество цена», например: <code>2000 5.5</code>\n/cancel — отмена")
    await call.answer()


@admin.message(Adm.price_add, F.text)
async def a_price_add_got(message: Message, state: FSMContext):
    try:
        s, p = message.text.split()
        stars, price = int(s), parse_price(p)
        if stars <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        return await message.answer("Неверный формат. Пример: <code>2000 5.5</code>")
    await state.clear()
    pk = get_packages()
    pk[stars] = price
    save_packages(pk)
    await message.answer(f"✅ Добавлено: {stars:,} Stars — {price} TON", reply_markup=back("a:prices"))


@admin.callback_query(F.data.startswith("a:pd:"))
async def a_price_delete(call: CallbackQuery):
    pk = get_packages()
    pk.pop(int(call.data.split(":")[2]), None)
    save_packages(pk)
    await aedit(call, "🗑 Пакет удалён", back("a:prices"))
    await call.answer()


# ---------- Превью (фото / видео / GIF) ----------
KIND_NAME = {"photo": "фото", "video": "видео", "animation": "GIF"}


@admin.callback_query(F.data == "a:prev")
async def a_prev(call: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for k, name in SECTIONS.items():
        pv = parse_preview(get_setting("preview_" + k))
        mark = f"✅ {KIND_NAME[pv[0]]}" if pv else "▫️"
        rows.append([B(f"{mark} · {name}", f"a:pv:{k}")])
    rows.append([B("⬅️ Назад", "a:menu")])
    await aedit(call, "🖼 <b>Превью разделов</b>\n\nФото, видео или GIF показываются над текстом раздела.", M(*rows))
    await call.answer()


@admin.callback_query(F.data.startswith("a:pv:"))
async def a_prev_card(call: CallbackQuery, state: FSMContext):
    k = call.data.split(":")[2]
    await state.set_state(Adm.wait_preview)
    await state.update_data(key=k)
    rows = []
    if get_setting("preview_" + k):
        rows.append([B("🗑 Убрать превью", f"a:pvd:{k}")])
    rows.append([B("⬅️ Назад", "a:prev")])
    await aedit(call, f"🖼 Раздел: <b>{SECTIONS[k]}</b>\n\nОтправь фото, видео или GIF, чтобы установить превью.", M(*rows))
    await call.answer()


@admin.message(Adm.wait_preview, F.photo | F.video | F.animation)
async def a_prev_got(message: Message, state: FSMContext):
    if message.animation:
        kind, fid = "animation", message.animation.file_id
    elif message.video:
        kind, fid = "video", message.video.file_id
    else:
        kind, fid = "photo", message.photo[-1].file_id
    d = await state.get_data()
    set_setting("preview_" + d["key"], f"{kind}:{fid}")
    await state.clear()
    await message.answer(f"✅ Превью ({KIND_NAME[kind]}) для «{SECTIONS[d['key']]}» установлено",
                         reply_markup=back("a:prev"))


@admin.message(Adm.wait_preview)
async def a_prev_bad(message: Message):
    await message.answer("Нужно отправить фото, видео или GIF (не файлом). /cancel — отмена")


@admin.callback_query(F.data.startswith("a:pvd:"))
async def a_prev_del(call: CallbackQuery, state: FSMContext):
    await state.clear()
    del_setting("preview_" + call.data.split(":")[2])
    await aedit(call, "🗑 Превью убрано", back("a:prev"))
    await call.answer()


# ---------- Premium Emoji ----------
@admin.callback_query(F.data == "a:emoji")
async def a_emoji(call: CallbackQuery, state: FSMContext):
    await state.clear()
    items = [B(f"{EMOJI[k]} {k} {'✅' if k in EMOJI_IDS else ''}", f"a:em:{k}") for k in EMOJI]
    rows = [items[i:i + 3] for i in range(0, len(items), 3)] + [[B("⬅️ Назад", "a:menu")]]
    note = "\n\n⚠️ Иконки в кнопках были отклонены Telegram и временно заменены обычными эмодзи (до перезапуска)." if ICONS_OFF else ""
    await aedit(call, "✨ <b>Premium Emoji</b>\n\nВыбери эмодзи, которое хочешь заменить на премиум. "
                      "Оно будет и в тексте, и в кнопках." + note, M(*rows))
    await call.answer()


@admin.callback_query(F.data.startswith("a:em:"))
async def a_emoji_card(call: CallbackQuery, state: FSMContext):
    k = call.data.split(":")[2]
    await state.set_state(Adm.wait_emoji)
    await state.update_data(key=k)
    rows = []
    if k in EMOJI_IDS:
        rows.append([B("♻️ Сбросить на обычное", f"a:emr:{k}")])
    rows.append([B("⬅️ Назад", "a:emoji")])
    await aedit(call, f"✨ <b>{k}</b> (сейчас: {e(k)})\n\nОтправь сообщение с премиум-эмодзи (нужен Telegram Premium) — будет взято первое.", M(*rows))
    await call.answer()


@admin.message(Adm.wait_emoji, F.text)
async def a_emoji_got(message: Message, state: FSMContext):
    ents = [en for en in (message.entities or []) if en.type == "custom_emoji"]
    if not ents:
        return await message.answer("В сообщении нет премиум-эмодзи. Попробуй ещё раз. /cancel — отмена")
    d = await state.get_data()
    eid = ents[0].custom_emoji_id
    set_setting("emoji_" + d["key"], eid)
    EMOJI_IDS[d["key"]] = eid
    await state.clear()
    await message.answer(f"✅ Сохранено: {e(d['key'])}\nID: <code>{eid}</code>", reply_markup=back("a:emoji"))


@admin.callback_query(F.data.startswith("a:emr:"))
async def a_emoji_reset(call: CallbackQuery, state: FSMContext):
    await state.clear()
    k = call.data.split(":")[2]
    del_setting("emoji_" + k)
    EMOJI_IDS.pop(k, None)
    await aedit(call, "♻️ Сброшено", back("a:emoji"))
    await call.answer()


# ======================= ЗАПУСК =======================
async def main():
    load_emoji()
    bot = SafeBot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(UserMiddleware())
    dp.include_router(top)
    dp.include_router(admin)
    dp.include_router(user)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
