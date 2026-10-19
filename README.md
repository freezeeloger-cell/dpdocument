# DP Document slots bot

Следит за списком «Обрати день» на `<центр>.pasport.org.ua/solutions/e-queue` (46 центров, 13 стран)
и шлёт подписчикам ссылку, когда появляются новые дни. Ничего не бронирует, логина нет.
Стек: python-telegram-bot + psycopg2 + Postgres + Playwright (Dockerfile).

## Railway
1. @BotFather → токен.
2. Новый проект из этого GitHub-репо (Railway соберёт по Dockerfile).
3. + Postgres в проект → в сервисе бота добавить переменную `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`.
4. Переменные: `BOT_TOKEN`, `ADMIN_IDS` (свой Telegram id). Опц.: `POLL_INTERVAL_MIN`/`MAX` (сек, по умолчанию 120/240).
5. Deploy. Таблицы создаются сами при старте.

## Проверка парсера
В логах после первой подписки должны быть проверки без `check <center> failed`.
Локально: `python checker.py varna` → `{"varna": {}}` = слотов нет (норма);
«найдено 0 select'ов» = Cloudflare не пускает или изменилась вёрстка.

## Telegram Stars (подписка)
Бесплатно приходит только «появились слоты: N дат». Даты, время и ссылка — по подписке.
Переменные: `STARS_PRICE` (по умолчанию 2500 ⭐/мес, лимит Telegram на подписку), `PREMIUM_DAYS` (30).
Подписка автопродляемая (Stars subscriptions), пользователь отменяет в Настройки → Мои звёзды.
Для теста без оплаты: админ пишет `/grant <chat_id> [дней]`.
Выплаты: Stars копятся на балансе бота, вывод в TON через 21 день (BotFather → бот → Balance).

## Команды
/start — страна → город · /centers — поменять · /stop — отписаться · /status — что видел бот по твоим центрам · /stats, /grant — админ
