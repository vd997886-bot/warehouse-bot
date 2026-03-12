import os
import re
import json
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
PHOTO_DB_PATH = "photos.json"

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

# временно храним, для какой детали ждём фото от какого пользователя
PENDING_PHOTO = {}


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

    serial = safe_str(row.get("SerialNumber"))
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


def load_photo_db() -> dict:
    if not os.path.exists(PHOTO_DB_PATH):
        return {}

    try:
        with open(PHOTO_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except Exception:
        return {}


def save_photo_db(data: dict):
    with open(PHOTO_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def send_part_response(update: Update, row):
    caption = fmt_row(row)
    part_number = safe_str(row.get("PartNumber"))
    norm_part = normalize_part_for_search(part_number)

    photo_db = load_photo_db()
    photo_file_id = photo_db.get(norm_part)

    if photo_file_id:
        try:
            await update.message.reply_photo(photo=photo_file_id, caption=caption)
            return
        except Exception:
            pass

    await update.message.reply_text(caption)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Просто отправь номер детали или часть номера.\n"
        "Чтобы обновить базу — отправь Excel файл .xlsx.\n\n"
        "Чтобы добавить фото детали:\n"
        "/photoadd ИД-3\n"
        "Потом просто отправь фото."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — старт\n"
        "/help — помощь\n"
        "/photoadd PARTNUMBER — добавить фото для детали\n"
        "/photo PARTNUMBER — показать фото детали\n"
        "/delphoto PARTNUMBER — удалить фото детали\n\n"
        "Поиск работает так:\n"
        "просто отправь номер детали или часть номера.\n"
        "Если для точного совпадения есть фото, я пришлю его сразу."
    )


async def photoadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /photoadd ИД-3")
        return

    part_raw = " ".join(context.args).strip()
    part_norm = normalize_part_for_search(part_raw)

    if not part_norm:
        await update.message.reply_text("Не вижу номер детали.")
        return

    PENDING_PHOTO[update.effective_user.id] = part_norm
    await update.message.reply_text(
        f"📸 Ок. Теперь отправь фото для детали: {part_raw}"
    )


async def photo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /photo ИД-3")
        return

    part_raw = " ".join(context.args).strip()
    part_norm = normalize_part_for_search(part_raw)

    photo_db = load_photo_db()
    photo_file_id = photo_db.get(part_norm)

    if not photo_file_id:
        await update.message.reply_text("❌ Фото для этой детали не найдено.")
        return

    try:
        await update.message.reply_photo(photo=photo_file_id, caption=f"📸 {part_raw}")
    except Exception:
        await update.message.reply_text("⚠️ Не получилось отправить фото.")


async def delphoto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /delphoto ИД-3")
        return

    part_raw = " ".join(context.args).strip()
    part_norm = normalize_part_for_search(part_raw)

    photo_db = load_photo_db()

    if part_norm in photo_db:
        del photo_db[part_norm]
        save_photo_db(photo_db)
        await update.message.reply_text(f"🗑 Фото для {part_raw} удалено.")
    else:
        await update.message.reply_text("❌ Для этой детали фото не найдено.")


async def handle_uploaded_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in PENDING_PHOTO:
        await update.message.reply_text(
            "Я получил фото, но не знаю для какой детали.\n"
            "Сначала напиши:\n"
            "/photoadd ИД-3"
        )
        return

    photos = update.message.photo
    if not photos:
        await update.message.reply_text("❌ Фото не найдено.")
        return

    best_photo = photos[-1]
    file_id = best_photo.file_id
    part_norm = PENDING_PHOTO[user_id]

    photo_db = load_photo_db()
    photo_db[part_norm] = file_id
    save_photo_db(photo_db)

    del PENDING_PHOTO[user_id]

    await update.message.reply_text("✅ Фото сохранено.")


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
    app.add_handler(CommandHandler("photoadd", photoadd_cmd))
    app.add_handler(CommandHandler("photo", photo_cmd))
    app.add_handler(CommandHandler("delphoto", delphoto_cmd))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_uploaded_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
