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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from centers import CENTERS, COUNTRIES, by_country, title, url
from checker import run_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
POLL_MIN = int(os.environ.get("POLL_INTERVAL_MIN", "120"))  # сек
POLL_MAX = int(os.environ.get("POLL_INTERVAL_MAX", "240"))


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


def subscribers(center) -> list[int]:
    return [r[0] for r in q("SELECT chat_id FROM subs WHERE center=%s", (center,))]


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
    return InlineKeyboardMarkup(_grid(btns, 2))


def kb_cities(chat_id, country):
    mine = user_centers(chat_id)
    btns = [InlineKeyboardButton(("✅ " if sub in mine else "") + city, callback_data=f"t:{sub}")
            for sub, (_, city, _) in by_country(country).items()]
    rows = _grid(btns, 2)
    rows.append([InlineKeyboardButton("◀️ Страны", callback_data="back")])
    return InlineKeyboardMarkup(rows)


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
    lines = []
    for c in sorted(mine):
        r = q("SELECT days, updated FROM center_state WHERE center=%s", (c,), one=True)
        if not r:
            lines.append(f"<b>{title(c)}</b>: ещё не проверял")
            continue
        age = int((time.time() - r[1]) / 60)
        lines.append(f"<b>{title(c)}</b>: {', '.join(r[0]) if r[0] else 'нет слотов'} <i>({age} мин назад)</i>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def stats(update: Update, ctx):
    if update.effective_chat.id not in ADMIN_IDS:
        return
    n_users = q("SELECT COUNT(*) FROM users", one=True)[0]
    n_subs = q("SELECT COUNT(DISTINCT chat_id) FROM subs", one=True)[0]
    per = q("SELECT center, COUNT(*) FROM subs GROUP BY center ORDER BY 2 DESC")
    await update.message.reply_text(
        f"Всего в боте: {n_users}\nС подписками: {n_subs}\n\n" + "\n".join(f"{title(c)}: {k}" for c, k in per))


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
        await cq.message.edit_text(my_list(cid) + "\n\nВыбери страну:", reply_markup=kb_countries(cid))
    elif data.startswith("t:"):
        sub = data[2:]
        if sub not in CENTERS:
            return await cq.answer("Нет такого центра")
        on = toggle(cid, sub)
        await cq.answer(("✅ Слежу за " if on else "❌ Больше не слежу за ") + title(sub))
        await cq.message.edit_reply_markup(reply_markup=kb_cities(cid, CENTERS[sub][0]))


# ---------- polling loop ----------
async def notify(app: Application, center: str, new_days: set[str], all_days: dict[str, list[str]]):
    lines = [f"🟢 <b>{title(center)}</b> — появились слоты!"]
    for d in sorted(all_days):
        t = all_days[d]
        lines.append(("🆕 " if d in new_days else "") + d
                     + (f": {', '.join(t[:8])}{'…' if len(t) > 8 else ''}" if t else ""))
    lines.append(f"\n👉 <a href=\"{url(center)}\">Записаться на сайте</a>\n"
                 f"<i>Слоты разбирают быстро. Для зарубежных центров нужна верификация через Дію или BankID.</i>")
    text = "\n".join(lines)
    sent = 0
    for chat_id in subscribers(center):
        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
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
                results = await run_once(centers)
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


async def post_init(app: Application):
    app.create_task(poll_loop(app))


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("centers", centers_cmd))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
