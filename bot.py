import os
import re
import json
import base64
import logging
from datetime import date, timedelta, datetime
from functools import wraps

import anthropic
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import database

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin")
CURRENCY = "€"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

ai_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# ── helpers ──────────────────────────────────────────────────────────────────

def fmt(amount: float) -> str:
    return f"{amount:,.2f} {CURRENCY}"


def week_bounds(weeks_ago: int = 0):
    today = date.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_ago)
    return monday, monday + timedelta(days=6)


def only_owner(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        return await func(update, context)
    return wrapper


async def build_stats_text(user_id: int, start: date, end: date, title: str) -> str:
    stats = await database.get_stats(user_id, start, end)
    cats = await database.get_category_breakdown(user_id, start, end)
    net = stats["income"] - stats["expenses"]
    lines = [
        f"📊 *{title}*",
        f"_{start.strftime('%d.%m')} — {end.strftime('%d.%m')}_\n",
        f"📈 Доходы:  *{fmt(stats['income'])}*",
        f"📉 Траты:   *{fmt(stats['expenses'])}*",
        f"💰 Итог:    *{fmt(net)}*",
    ]
    if cats:
        lines.append("\n*По категориям:*")
        for cat, total in cats:
            lines.append(f"  • {cat}: {fmt(total)}")
    return "\n".join(lines)


# ── commands ──────────────────────────────────────────────────────────────────

@only_owner
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Скидывай сюда траты, доходы или *фото чека* — всё запишу.\n\n"
        "Текстом:\n"
        "  `50 кофе` — трата 50€\n"
        "  `+2000 зарплата` — доход 2000€\n"
        "  `15.50 обед` — с копейками\n\n"
        "Фото: просто отправь снимок чека или экрана оплаты.\n\n"
        "Команды:\n"
        "  /balance — текущий баланс\n"
        "  /week — итог этой недели\n"
        "  /stats — итог месяца\n"
        "  /history — последние 10 операций\n"
        "  /del 5 — удалить запись №5",
        parse_mode="Markdown",
    )


@only_owner
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = await database.get_balance(update.effective_user.id)
    sign = "✅" if bal >= 0 else "🔴"
    await update.message.reply_text(
        f"{sign} Баланс: *{fmt(bal)}*", parse_mode="Markdown"
    )


@only_owner
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start, end = week_bounds(0)
    text = await build_stats_text(update.effective_user.id, start, end, "Эта неделя")
    await update.message.reply_text(text, parse_mode="Markdown")


@only_owner
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today()
    start = today.replace(day=1)
    title = today.strftime("%m.%Y")
    text = await build_stats_text(update.effective_user.id, start, today, title)
    await update.message.reply_text(text, parse_mode="Markdown")


@only_owner
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await database.get_history(update.effective_user.id, 10)
    if not rows:
        await update.message.reply_text("История пуста.")
        return
    lines = ["*Последние операции:*\n"]
    for tx_id, amount, category, created_at in rows:
        arrow = "📈" if amount > 0 else "📉"
        dt = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
        lines.append(f"`#{tx_id}` {arrow} {fmt(abs(amount))} — {category} _{dt}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@only_owner
async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "Укажи номер: `/del 5`", parse_mode="Markdown"
        )
        return
    tx_id = int(context.args[0])
    ok = await database.delete_transaction(update.effective_user.id, tx_id)
    if ok:
        await update.message.reply_text(f"✅ Запись #{tx_id} удалена.")
    else:
        await update.message.reply_text(f"❌ Запись #{tx_id} не найдена.")


# ── text message handler ──────────────────────────────────────────────────────

@only_owner
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    m = re.match(r'^([+\-]?\d+(?:[.,]\d{1,2})?)\s*(.*)$', text)
    if not m:
        await update.message.reply_text(
            "Не понял 🤔 Напиши, например:\n`50 кофе` или `+1500 зарплата`\n"
            "Или скинь фото чека.",
            parse_mode="Markdown",
        )
        return

    raw = float(m.group(1).replace(",", "."))
    category = m.group(2).strip() or "прочее"
    is_income = text.startswith("+")
    stored = raw if is_income else -abs(raw)

    uid = update.effective_user.id
    tx_id = await database.add_transaction(uid, stored, category)
    bal = await database.get_balance(uid)

    label, arrow = ("Доход", "📈") if is_income else ("Трата", "📉")
    await update.message.reply_text(
        f"{arrow} {label}: *{fmt(abs(raw))}* — {category}\n"
        f"_#{tx_id} · Баланс: {fmt(bal)}_",
        parse_mode="Markdown",
    )


# ── photo handler (Claude Vision) ─────────────────────────────────────────────

@only_owner
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ai_client:
        await update.message.reply_text(
            "📸 Распознавание фото пока не настроено.\nНапиши трату текстом: `50 кофе`",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text("📸 Смотрю на чек…")

    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    photo_b64 = base64.standard_b64encode(photo_bytes).decode()

    try:
        response = await ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": photo_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Это фото чека или платёжного экрана. "
                                "Найди итоговую сумму и кратко опиши что куплено (1-3 слова на русском). "
                                "Если валюта не евро — переводить не нужно, просто укажи число. "
                                "Ответь ТОЛЬКО JSON без пояснений: "
                                '{"amount": 12.50, "category": "кофе"}'
                            ),
                        },
                    ],
                }
            ],
        )
        raw_json = response.content[0].text.strip()
        # Strip markdown code block if Claude wrapped it
        raw_json = re.sub(r"^```[a-z]*\n?", "", raw_json)
        raw_json = re.sub(r"\n?```$", "", raw_json)
        parsed = json.loads(raw_json)
        amount = float(parsed["amount"])
        category = str(parsed.get("category", "прочее"))
    except Exception as e:
        logger.error("Photo parse error: %s", e)
        await update.message.reply_text(
            "Не смог разобрать чек 😕 Попробуй написать вручную:\n`50 кофе`",
            parse_mode="Markdown",
        )
        return

    context.user_data["pending"] = {"amount": amount, "category": category}

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Записать", callback_data="photo_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="photo_cancel"),
        ]
    ])
    await update.message.reply_text(
        f"📸 Вижу трату: *{fmt(amount)}* — {category}\n\nЗаписать?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@only_owner
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "photo_confirm":
        pending = context.user_data.pop("pending", None)
        if not pending:
            await query.edit_message_text("Что-то пошло не так, попробуй ещё раз.")
            return
        uid = query.from_user.id
        tx_id = await database.add_transaction(uid, -pending["amount"], pending["category"])
        bal = await database.get_balance(uid)
        await query.edit_message_text(
            f"✅ Записал: *{fmt(pending['amount'])}* — {pending['category']}\n"
            f"_#{tx_id} · Баланс: {fmt(bal)}_",
            parse_mode="Markdown",
        )

    elif query.data == "photo_cancel":
        context.user_data.pop("pending", None)
        await query.edit_message_text("❌ Отменено.")


# ── scheduled reports ─────────────────────────────────────────────────────────

async def send_scheduled_report(bot, is_friday: bool):
    weeks_ago = 0 if is_friday else 1
    start, end = week_bounds(weeks_ago)
    title = "Итог недели (промежуточный)" if is_friday else "Итог прошлой недели"
    text = await build_stats_text(ALLOWED_USER_ID, start, end, title)
    await bot.send_message(ALLOWED_USER_ID, text, parse_mode="Markdown")


# ── app lifecycle ─────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    await database.init_db()
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        send_scheduled_report, "cron",
        day_of_week="fri", hour=18, minute=0,
        kwargs={"bot": application.bot, "is_friday": True},
    )
    scheduler.add_job(
        send_scheduled_report, "cron",
        day_of_week="mon", hour=9, minute=0,
        kwargs={"bot": application.bot, "is_friday": False},
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler


async def post_shutdown(application: Application) -> None:
    application.bot_data["scheduler"].shutdown(wait=False)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling()


if __name__ == "__main__":
    main()
