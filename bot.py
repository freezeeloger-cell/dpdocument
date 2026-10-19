"""
Бот уведомлений о слотах ДП «Документ».
Стек как в @Crabnorwaybot: python-telegram-bot + psycopg2 + Postgres на Railway.
Пользователь выбирает страну → город (несколько). Бот ничего не бронирует.
"""
import asyncio
import json
import logging
import os
import random
import time

import psycopg2
import psycopg2.extras
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PreCheckoutQueryHandler, filters)

from centers import CENTERS, COUNTRIES, by_country, title, url
from checker import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # не светить токен в логах
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
POLL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "120"))  # сек
POLL_MAX = int(os.environ.get("POLL_INTERVAL_MAX", "240"))
GROUP_URL = os.environ.get("GROUP_URL", "https://t.me/DpdocSupport")
NUDGE_HOURS = int(os.environ.get("NUDGE_HOURS", "48"))
STARS_PRICE = int(os.environ.get("STARS_PRICE", "2500"))      # ⭐ в месяц (лимит Telegram на подписку ~2500)
PREMIUM_DAYS = int(os.environ.get("PREMIUM_DAYS", "30"))
SUB_PERIOD = 30 * 24 * 60 * 60                                 # единственный разрешённый период подписки


# ---------- db ----------
def db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_notified_at TIMESTAMPTZ;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS nudged BOOLEAN DEFAULT FALSE;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ;
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT, stars INT, charge_id TEXT, recurring BOOLEAN,
                expires_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS subs (
                chat_id BIGINT,
                center TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (chat_id, center)
            );
            CREATE TABLE IF NOT EXISTS center_state (
                center TEXT PRIMARY KEY,
                days JSONB NOT NULL DEFAULT '[]',
                updated REAL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                center TEXT, days JSONB, sent_to INT, created_at TIMESTAMPTZ DEFAULT now()
            );
        """)


def q(sql, args=(), one=False, fetch=True):
    with db() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, args)
        if not fetch:
            return None
        return cur.fetchone() if one else cur.fetchall()


def upsert_user(u):
    q("INSERT INTO users (chat_id, username, first_name) VALUES (%s,%s,%s) "
      "ON CONFLICT (chat_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name",
      (u.id, u.username, u.first_name), fetch=False)


def user_centers(chat_id) -> set[str]:
    return {r[0] for r in q("SELECT center FROM subs WHERE chat_id=%s", (chat_id,))}


def toggle(chat_id, center) -> bool:
    if center in user_centers(chat_id):
        q("DELETE FROM subs WHERE chat_id=%s AND center=%s", (chat_id, center), fetch=False)
        return False
    q("INSERT INTO subs (chat_id, center) VALUES (%s,%s) ON CONFLICT DO NOTHING", (chat_id, center), fetch=False)
    return True


def subscribers(center) -> list[tuple[int, bool]]:
    """[(chat_id, is_premium)]"""
    return [(r[0], bool(r[1])) for r in q(
        "SELECT s.chat_id, (u.premium_until IS NOT NULL AND u.premium_until > now()) "
        "FROM subs s LEFT JOIN users u ON u.chat_id = s.chat_id WHERE s.center=%s", (center,))]


def is_premium(chat_id) -> bool:
    r = q("SELECT premium_until > now() FROM users WHERE chat_id=%s", (chat_id,), one=True)
    return bool(r and r[0])


def premium_until(chat_id):
    r = q("SELECT premium_until FROM users WHERE chat_id=%s", (chat_id,), one=True)
    return r[0] if r else None


def grant_premium(chat_id, days: int, until=None):
    if until is not None:
        q("UPDATE users SET premium_until=%s WHERE chat_id=%s", (until, chat_id), fetch=False)
    else:
        q("UPDATE users SET premium_until = GREATEST(COALESCE(premium_until, now()), now()) + (%s * interval '1 day') "
          "WHERE chat_id=%s", (days, chat_id), fetch=False)


def active_centers() -> list[str]:
    return [r[0] for r in q("SELECT DISTINCT center FROM subs")]


def prev_days(center) -> set[str]:
    r = q("SELECT days FROM center_state WHERE center=%s", (center,), one=True)
    return set(r[0]) if r else set()


def save_days(center, days: set[str]):
    q("INSERT INTO center_state (center, days, updated) VALUES (%s,%s,%s) "
      "ON CONFLICT (center) DO UPDATE SET days=EXCLUDED.days, updated=EXCLUDED.updated",
      (center, json.dumps(sorted(days), ensure_ascii=False), time.time()), fetch=False)


# ---------- keyboards ----------
def _grid(btns, cols):
    return [btns[i:i + cols] for i in range(0, len(btns), cols)]


def kb_countries(chat_id):
    mine = user_centers(chat_id)
    btns = []
    for i, c in enumerate(COUNTRIES):
        n = sum(1 for s in by_country(c) if s in mine)
        btns.append(InlineKeyboardButton(c + (f" ({n})" if n else ""), callback_data=f"c:{i}"))
    rows = _grid(btns, 2)
    if mine:
        rows.append([InlineKeyboardButton("🔔 Получать уведомления", callback_data="done")])
    return InlineKeyboardMarkup(rows)


def kb_cities(chat_id, country):
    mine = user_centers(chat_id)
    btns = [InlineKeyboardButton(("✅ " if sub in mine else "") + city, callback_data=f"t:{sub}")
            for sub, (_, city, _) in by_country(country).items()]
    rows = _grid(btns, 2)
    rows.append([InlineKeyboardButton("◀️ Страны", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def kb_group(extra: list[InlineKeyboardButton] | None = None):
    rows = []
    if extra:
        rows.append(extra)
    rows.append([InlineKeyboardButton("💬 Группа с ответами по документам", url=GROUP_URL)])
    return InlineKeyboardMarkup(rows)


def btn_buy():
    return InlineKeyboardButton(f"🔓 Открыть даты и ссылку — {STARS_PRICE} ⭐ / мес", callback_data="buy")


def full_text(center: str, all_days: dict[str, list[str]], new_days: set[str] = frozenset()) -> str:
    lines = [f"🟢 <b>{title(center)}</b> — есть свободные слоты!"]
    for d in sorted(all_days):
        t = all_days[d]
        lines.append(("🆕 " if d in new_days else "") + d
                     + (f": {', '.join(t[:8])}{'…' if len(t) > 8 else ''}" if t else ""))
    lines.append("\n<i>Слоты разбирают быстро. Для зарубежных центров нужна верификация через Дію или BankID. "
                 "Вопросы — в группе.</i>")
    return "\n".join(lines)


def teaser_text(center: str, all_days: dict[str, list[str]]) -> str:
    n = len(all_days)
    word = "дата" if n == 1 else ("даты" if 2 <= n <= 4 else "дат")
    return (f"🟢 <b>{title(center)}</b> — появились свободные слоты: <b>{n} {word}</b>.\n\n"
            f"Даты, время и ссылка на запись — в полной версии уведомлений.\n"
            f"Подписка {STARS_PRICE} ⭐ в месяц: все уведомления по твоим центрам приходят сразу с датами и ссылкой.")


async def send_invoice(bot, chat_id):
    link = await bot.create_invoice_link(
        title="Уведомления о слотах ДП «Документ»",
        description=f"{PREMIUM_DAYS} дней: даты, время и ссылка на запись в каждом уведомлении по твоим центрам.",
        payload=f"premium:{chat_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Подписка на месяц", STARS_PRICE)],
        subscription_period=SUB_PERIOD,
    )
    await bot.send_message(
        chat_id,
        f"Подписка на месяц — {STARS_PRICE} ⭐. Продлевается автоматически, отменить можно в настройках Telegram "
        f"(Настройки → Мои звёзды).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Оплатить {STARS_PRICE} ⭐", url=link)]]),
    )


def my_list(chat_id) -> str:
    mine = user_centers(chat_id)
    if not mine:
        return "Пока ни одного центра не выбрано."
    return "Слежу за: " + ", ".join(title(s) for s in sorted(mine))


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user)
    await update.message.reply_text(
        "Я слежу за свободными слотами в электронной очереди ДП «Документ» и пришлю ссылку, "
        "как только они появятся. Бронируешь ты сам на сайте.\n\nВыбери страну:",
        reply_markup=kb_countries(update.effective_chat.id),
    )


async def centers_cmd(update: Update, ctx):
    cid = update.effective_chat.id
    await update.message.reply_text(my_list(cid) + "\n\nВыбери страну:", reply_markup=kb_countries(cid))


async def stop(update: Update, ctx):
    q("DELETE FROM subs WHERE chat_id=%s", (update.effective_chat.id,), fetch=False)
    await update.message.reply_text("Отписал от всех центров. /start — чтобы включить снова.")


async def status(update: Update, ctx):
    mine = user_centers(update.effective_chat.id)
    if not mine:
        return await update.message.reply_text("Ты ни на что не подписан. /centers")
    prem = is_premium(update.effective_chat.id)
    lines = []
    for c in sorted(mine):
        r = q("SELECT days, updated FROM center_state WHERE center=%s", (c,), one=True)
        if not r:
            lines.append(f"<b>{title(c)}</b>: ещё не проверял")
            continue
        age = int((time.time() - r[1]) / 60)
        if not r[0]:
            what = "нет слотов"
        elif prem:
            what = ", ".join(r[0])
        else:
            what = f"есть слоты ({len(r[0])} дат) — детали по подписке"
        lines.append(f"<b>{title(c)}</b>: {what} <i>({age} мин назад)</i>")
    markup = None if prem else InlineKeyboardMarkup([[btn_buy()]])
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=markup)


async def stats(update: Update, ctx):
    if update.effective_chat.id not in ADMIN_IDS:
        return
    n_users = q("SELECT COUNT(*) FROM users", one=True)[0]
    n_subs = q("SELECT COUNT(DISTINCT chat_id) FROM subs", one=True)[0]
    n_prem = q("SELECT COUNT(*) FROM users WHERE premium_until > now()", one=True)[0]
    stars = q("SELECT COALESCE(SUM(stars),0) FROM payments", one=True)[0]
    per = q("SELECT center, COUNT(*) FROM subs GROUP BY center ORDER BY 2 DESC")
    await update.message.reply_text(
        f"Всего в боте: {n_users}\nС подписками: {n_subs}\nПремиум активных: {n_prem}\nЗвёзд всего: {stars}\n\n"
        + "\n".join(f"{title(c)}: {k}" for c, k in per))


async def premium_cmd(update: Update, ctx):
    cid = update.effective_chat.id
    upsert_user(update.effective_user)
    until = premium_until(cid)
    if until and until.timestamp() > time.time():
        return await update.message.reply_text(f"Подписка активна до {until.strftime('%d.%m.%Y')}.")
    await send_invoice(ctx.bot, cid)


async def grant_cmd(update: Update, ctx):
    """/grant <chat_id> [дней] — выдать премиум вручную (для теста). Только админ."""
    if update.effective_chat.id not in ADMIN_IDS:
        return
    try:
        target = int(ctx.args[0]); days = int(ctx.args[1]) if len(ctx.args) > 1 else PREMIUM_DAYS
    except (IndexError, ValueError):
        return await update.message.reply_text("/grant <chat_id> [дней]")
    q("INSERT INTO users (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (target,), fetch=False)
    grant_premium(target, days)
    await update.message.reply_text(f"Выдал {days} дн. для {target}, до {premium_until(target):%d.%m.%Y}")
    try:
        await ctx.bot.send_message(target, f"✅ Тебе включили полные уведомления на {days} дн.")
    except Exception:  # noqa: BLE001
        pass


async def precheckout(update: Update, ctx):
    await update.pre_checkout_query.answer(ok=True)


async def on_paid(update: Update, ctx):
    sp = update.message.successful_payment
    cid = update.effective_chat.id
    upsert_user(update.effective_user)
    exp = sp.subscription_expiration_date  # datetime или None
    if exp:
        grant_premium(cid, PREMIUM_DAYS, until=exp)
    else:
        grant_premium(cid, PREMIUM_DAYS)
    q("INSERT INTO payments (chat_id, stars, charge_id, recurring, expires_at) VALUES (%s,%s,%s,%s,%s)",
      (cid, sp.total_amount, sp.telegram_payment_charge_id, bool(sp.is_recurring), exp), fetch=False)
    log.info("payment %s stars from %s recurring=%s", sp.total_amount, cid, sp.is_recurring)
    until = premium_until(cid)
    await update.message.reply_text(
        f"✅ Оплачено. Полные уведомления включены до {until:%d.%m.%Y}.\n"
        f"Сейчас пришлю, что вижу по твоим центрам.")
    # сразу показать текущее состояние
    for c in sorted(user_centers(cid)):
        r = q("SELECT days FROM center_state WHERE center=%s", (c,), one=True)
        if r and r[0]:
            await ctx.bot.send_message(cid, full_text(c, {d: [] for d in r[0]}), parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True,
                                       reply_markup=kb_group([InlineKeyboardButton("📝 Записаться на сайте", url=url(c))]))
    for a in ADMIN_IDS:
        try:
            await ctx.bot.send_message(a, f"💫 Оплата {sp.total_amount} ⭐ от {cid} (@{update.effective_user.username})")
        except Exception:  # noqa: BLE001
            pass


async def on_callback(update: Update, ctx):
    cq = update.callback_query
    cid = cq.message.chat.id
    data = cq.data
    if data.startswith("c:"):
        country = COUNTRIES[int(data[2:])]
        await cq.answer()
        await cq.message.edit_text(f"{country} — выбери город (можно несколько):", reply_markup=kb_cities(cid, country))
    elif data == "back":
        await cq.answer()
        await cq.message.edit_text(my_list(cid) + "\n\nВыбери страну или нажми «Получать уведомления»:",
                                   reply_markup=kb_countries(cid))
    elif data == "done":
        mine = user_centers(cid)
        if not mine:
            return await cq.answer("Сначала выбери хотя бы один город", show_alert=True)
        await cq.answer()
        await cq.message.edit_text(my_list(cid))
        await cq.message.reply_text(
            "✅ Всё включено.\n\n"
            "Я проверяю очередь каждые пару минут. Как только в твоих центрах появятся свободные слоты — "
            "сразу пришлю ссылку. Новые талоны обычно выкладывают в течение дня, в случайное время, "
            "поэтому держи уведомления включёнными.\n\n"
            "Бронируешь ты сам на сайте — для зарубежных центров нужна Дія или BankID.\n\n"
            "Вопросы по документам, записи, Дії или BankID — в нашей группе, там отвечают.\n\n"
            f"Бесплатно: сообщу, что слоты появились. По подписке ({STARS_PRICE} ⭐/мес): "
            "сразу даты, время и ссылка на запись — /premium.\n\n"
            "/centers — поменять города · /status — что я вижу сейчас · /stop — отписаться",
            reply_markup=kb_group([btn_buy()]),
        )
    elif data == "buy":
        await cq.answer()
        if is_premium(cid):
            return await cq.message.reply_text(f"Подписка уже активна до {premium_until(cid):%d.%m.%Y}.")
        await send_invoice(ctx.bot, cid)
    elif data.startswith("t:"):
        sub = data[2:]
        if sub not in CENTERS:
            return await cq.answer("Нет такого центра")
        on = toggle(cid, sub)
        await cq.answer(("✅ Слежу за " if on else "❌ Больше не слежу за ") + title(sub))
        await cq.message.edit_reply_markup(reply_markup=kb_cities(cid, CENTERS[sub][0]))


# ---------- polling loop ----------
async def notify(app: Application, center: str, new_days: set[str], all_days: dict[str, list[str]]):
    text_full = full_text(center, all_days, new_days)
    kb_full = kb_group([InlineKeyboardButton("📝 Записаться на сайте", url=url(center))])
    text_teaser = teaser_text(center, all_days)
    kb_teaser = kb_group([btn_buy()])
    sent = 0
    for chat_id, prem in subscribers(center):
        try:
            if prem:
                await app.bot.send_message(chat_id, text_full, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True, reply_markup=kb_full)
            else:
                await app.bot.send_message(chat_id, text_teaser, parse_mode=ParseMode.HTML,
                                           disable_web_page_preview=True, reply_markup=kb_teaser)
            q("UPDATE users SET last_notified_at=now() WHERE chat_id=%s", (chat_id,), fetch=False)
            sent += 1
            await asyncio.sleep(0.05)
        except Forbidden:
            q("DELETE FROM subs WHERE chat_id=%s", (chat_id,), fetch=False)
        except BadRequest as e:
            log.info("send to %s failed: %s", chat_id, e)
    q("INSERT INTO notifications (center, days, sent_to) VALUES (%s,%s,%s)",
      (center, json.dumps(sorted(new_days), ensure_ascii=False), sent), fetch=False)


async def poll_loop(app: Application):
    await asyncio.sleep(5)
    fails: dict[str, int] = {}
    while True:
        try:
            centers = active_centers()
            if centers:
                log.info("checking %d centers: %s", len(centers), ", ".join(centers))
                results = await run_once(centers)
                for c, res in results.items():
                    if not isinstance(res, Exception):
                        log.info("%s: %d days", c, len(res))
                for c, res in results.items():
                    if isinstance(res, Exception):
                        fails[c] = fails.get(c, 0) + 1
                        if fails[c] in (3, 20):
                            for a in ADMIN_IDS:
                                await app.bot.send_message(a, f"⚠️ {c}: {fails[c]} ошибок подряд: {res}")
                        continue
                    fails[c] = 0
                    now = set(res)
                    new = now - prev_days(c)
                    if new:
                        log.info("%s: new days %s", c, new)
                        await notify(app, c, new, res)
                    save_days(c, now)
        except Exception:  # noqa: BLE001
            log.exception("poll loop error")
        await asyncio.sleep(random.uniform(POLL_MIN, POLL_MAX))


async def nudge_loop(app: Application):
    """Один раз напомнить тем, кому за NUDGE_HOURS ни разу не пришло уведомление."""
    await asyncio.sleep(60)
    while True:
        try:
            rows = q("""
                SELECT u.chat_id FROM users u
                WHERE u.nudged = FALSE AND u.last_notified_at IS NULL
                  AND EXISTS (SELECT 1 FROM subs s WHERE s.chat_id = u.chat_id)
                  AND (SELECT MIN(created_at) FROM subs s WHERE s.chat_id = u.chat_id) < now() - (%s * interval '1 hour')
            """, (NUDGE_HOURS,))
            for (chat_id,) in rows:
                try:
                    await app.bot.send_message(
                        chat_id,
                        "Пока в твоих центрах слотов не было — я продолжаю следить, как появятся, сразу напишу.\n\n"
                        "Пока ждёшь, проверь, что документы и Дія/BankID готовы — чтобы успеть забронировать, "
                        "когда придёт уведомление. Вопросы — в группе.",
                        reply_markup=kb_group(),
                    )
                except Forbidden:
                    q("DELETE FROM subs WHERE chat_id=%s", (chat_id,), fetch=False)
                except BadRequest as e:
                    log.info("nudge %s failed: %s", chat_id, e)
                q("UPDATE users SET nudged=TRUE WHERE chat_id=%s", (chat_id,), fetch=False)
                await asyncio.sleep(0.05)
        except Exception:  # noqa: BLE001
            log.exception("nudge loop error")
        await asyncio.sleep(3600)


async def post_init(app: Application):
    loop = asyncio.get_running_loop()
    loop.create_task(poll_loop(app))
    loop.create_task(nudge_loop(app))
    log.info("poll + nudge loops scheduled")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("centers", centers_cmd))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
