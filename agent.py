import os
import re
import difflib
import pandas as pd

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("TOKEN")
FILE_PATH = "warehouse.xlsx"

REQUIRED_COLUMNS = [
    "PartNumber",
    "Quantity",
    "Shelf",
    "Location",
    "Passport",
    "Category",
    "SerialNumber",
    "Check",
    "Price",
]


def normalize_part_for_search(value: str) -> str:
    if value is None:
        return ""
    value = str(value).strip().upper()
    value = re.sub(r"[\s\-_./\\]+", "", value)
    return value


def safe_str(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def load_df() -> pd.DataFrame:
    if not os.path.exists(FILE_PATH):
        raise FileNotFoundError(
            f"Файл {FILE_PATH} не найден. Пришли .xlsx файлом в бота, чтобы загрузить таблицу."
        )

    df = pd.read_excel(FILE_PATH)
    df.columns = [str(col).strip() for col in df.columns]

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError("В Excel не хватает колонок:\n" + ", ".join(missing))

    df["PartNumber"] = df["PartNumber"].astype(str)
    df["_pn_norm"] = df["PartNumber"].apply(normalize_part_for_search)

    return df


def fmt_row(row) -> str:
    part = safe_str(row.get("PartNumber"))
    qty = safe_str(row.get("Quantity"))
    shelf = safe_str(row.get("Shelf"))
    location = safe_str(row.get("Location"))
    passport = safe_str(row.get("Passport"))
    category = safe_str(row.get("Category"))
    serial = safe_str(row.get("SerialNumber"))
    check = safe_str(row.get("Check"))
    price = safe_str(row.get("Price"))

    if not price:
        price = "—"
    if not check:
        check = "не проверена"

    return (
        f"✅ {part} есть в наличии\n"
        f"📦 Полка: {shelf}, ячейка: {location}\n"
        f"🔢 Количество: {qty}\n"
        f"📄 Паспорт: {passport}\n"
        f"🆕 Категория: {category}\n"
        f"💰 Цена: {price}\n"
        f"🔑 Серийный номер: {serial}\n"
        f"✔ Проверка: {check}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Просто отправь номер детали или часть номера.\n"
        "Чтобы обновить базу — отправь Excel файл .xlsx."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — старт\n"
        "/help — помощь\n\n"
        "1) Поиск: просто отправь PartNumber или часть номера\n"
        "2) Обновление: отправь .xlsx файлом — я заменю warehouse.xlsx"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return

    name = doc.file_name or ""
    if not name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Пришли именно Excel файл (.xlsx)")
        return

    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(FILE_PATH)

    try:
        _ = load_df()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Файл скачался, но есть ошибка:\n{e}")
        return

    await update.message.reply_text("✅ Таблица обновлена! Теперь можно искать.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    query_norm = normalize_part_for_search(text)

    try:
        df = load_df()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")
        return

    # 1) Сначала точное совпадение
    exact_only = df[df["_pn_norm"] == query_norm]

    if not exact_only.empty:
        responses = [fmt_row(row) for _, row in exact_only.head(3).iterrows()]
        await update.message.reply_text("\n\n".join(responses))
        return

    # 2) Потом частичное совпадение
    partial = df[df["_pn_norm"].str.contains(query_norm, na=False)]

    if not partial.empty:
        responses = [fmt_row(row) for _, row in partial.head(3).iterrows()]
        msg = "\n\n".join(responses)

        if len(partial) > 3:
            msg += "\n\nℹ️ Нашла несколько вариантов, показала первые 3."
        await update.message.reply_text(msg)
        return

    # 3) Потом похожие
    pn_list = df["_pn_norm"].dropna().tolist()
    close = difflib.get_close_matches(query_norm, pn_list, n=3, cutoff=0.75)

    if close:
        fuzzy = df[df["_pn_norm"].isin(close)]
        responses = [fmt_row(row) for _, row in fuzzy.head(3).iterrows()]
        msg = "🤔 Точного совпадения нет, но нашла похожие:\n\n" + "\n\n".join(responses)
        await update.message.reply_text(msg)
        return

    await update.message.reply_text("❓ Ничего не нашла по этому запросу")


def main():
    if not TOKEN:
        raise RuntimeError("TOKEN не задан. Добавь TOKEN в Railway Variables.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
