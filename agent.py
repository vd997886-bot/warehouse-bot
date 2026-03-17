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
    "PhotoID",
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


def translate_value(value, field):
    v = safe_str(value).lower()

    if field == "passport":
        if v in ["yes", "y", "true", "1"]:
            return "есть"
        if v in ["no", "n", "false", "0"]:
            return "нет"

    if field == "check":
        if v in ["yes", "y", "true", "1"]:
            return "проверена"
        if v in ["no", "n", "false", "0"]:
            return "не проверена"

    if field == "category":
        if v == "new":
            return "новая"
        if v == "used":
            return "б/у"
        if v == "serviceable":
            return "исправная"
        if v == "overhauled":
            return "после ремонта"

    return safe_str(value)


def clean_serial(value) -> str:
    serial = safe_str(value)
    if serial in ["/", "-", "—"]:
        return "—"
    return serial


def clean_price(value) -> str:
    price = safe_str(value)

    if not price or price in ["/", "-", "—"]:
        return "—"

    price = price.replace("USD", "$").replace("usd", "$").strip()
    return price


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

    passport = translate_value(row.get("Passport"), "passport")
    category = translate_value(row.get("Category"), "category")
    check = translate_value(row.get("Check"), "check")

    serial = clean_serial(row.get("SerialNumber"))
    price = clean_price(row.get("Price"))

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


async def send_part_response(update: Update, row):
    caption = fmt_row(row)
    photo_id = safe_str(row.get("PhotoID"))

    if photo_id:
        try:
            await update.message.reply_photo(photo=photo_id, caption=caption)
            return
        except Exception as e:
            print("reply_photo error:", e)

    await update.message.reply_text(caption)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Просто отправь номер детали или часть номера.\n"
        "Чтобы обновить базу — отправь Excel файл .xlsx.\n\n"
        "Если хочешь добавить фото:\n"
        "просто отправь мне фотографию, и я пришлю PhotoID."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — старт\n"
        "/help — помощь\n\n"
        "Поиск:\n"
        "просто отправь номер детали или часть номера.\n\n"
        "Обновление базы:\n"
        "отправь Excel файл .xlsx\n\n"
        "Фото:\n"
        "отправь боту фотографию, и я пришлю PhotoID.\n"
        "Потом вставь этот PhotoID в колонку PhotoID в Excel."
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


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    await update.message.reply_text(
        f"PhotoID:\n{file_id}\n\nСкопируй это и вставь в колонку PhotoID в Excel."
    )


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

    # 1) точное совпадение
    exact_only = df[df["_pn_norm"] == query_norm]

    if not exact_only.empty:
        row = exact_only.iloc[0]
        await send_part_response(update, row)
        return

    # 2) частичное совпадение
    partial = df[df["_pn_norm"].str.contains(query_norm, na=False)]

    if not partial.empty:
        responses = [fmt_row(row) for _, row in partial.head(3).iterrows()]
        msg = "\n\n".join(responses)

        if len(partial) > 3:
            msg += "\n\nℹ️ Нашла несколько вариантов, показала первые 3."
        await update.message.reply_text(msg)
        return

    # 3) похожие
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
