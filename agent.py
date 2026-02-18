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

# ========== SETTINGS ==========
TOKEN = os.getenv("TOKEN")  # set in Railway Variables
FILE_PATH = "warehouse.xlsx"

REQUIRED_COLUMNS = {
    "PartNumber",
    "Quantity",
    "Shelf",
    "Location",
    "Passport",
    "Category",
    "SerialNumber",
    "Check",
    "Price",   # ✅ добавили цену
}
# ==============================


def normalize_text(v) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def to_yes(v) -> bool:
    s = normalize_text(v).lower()
    return s in {"yes", "y", "true", "1", "да", "ok", "checked", "есть"}


def normalize_part_for_search(s: str) -> str:
    """
    Делает поиск "похожего" лучше:
    - нижний регистр
    - убирает пробелы, дефисы, слеши, точки
    """
    s = normalize_text(s).lower()
    s = re.sub(r"[ \t\r\n\-\._/\\]+", "", s)
    return s


def safe_int(v) -> int:
    try:
        if pd.isna(v):
            return 0
        return int(float(v))
    except Exception:
        return 0


def fmt_price(v) -> str:
    """
    Красиво показывает цену:
    - пусто -> "—"
    - число -> без .0
    """
    if pd.isna(v):
        return "—"
    s = str(v).strip()
    if not s:
        return "—"
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
        return str(f)
    except Exception:
        return s


def fmt_row(row) -> str:
    part = normalize_text(row["PartNumber"])
    qty = safe_int(row["Quantity"])
    shelf = normalize_text(row["Shelf"])
    location = normalize_text(row["Location"])

    passport = "есть" if to_yes(row["Passport"]) else "нет"

    cat_raw = normalize_text(row["Category"]).lower()
    if cat_raw in {"new", "нова", "новая"}:
        category = "новая"
    elif cat_raw in {"old", "стара", "старая"}:
        category = "старая"
    else:
        category = normalize_text(row["Category"]) or "—"

    serial = normalize_text(row["SerialNumber"]) or "—"
    checked = "проверена" if to_yes(row["Check"]) else "не проверена"

    price = fmt_price(row["Price"])  # ✅ цена

    if qty > 0:
        return (
            f"✅ {part} есть в наличии\n"
            f"📦 Полка: {shelf}, ячейка: {location}\n"
            f"🔢 Количество: {qty}\n"
            f"📄 Паспорт: {passport}\n"
            f"🆕 Категория: {category}\n"
            f"💰 Цена: {price}\n"          # ✅ добавили
            f"🔑 Серийный номер: {serial}\n"
            f"✔️ Проверка: {checked}"
        )
    else:
        return (
            f"❌ {part} нет в наличии\n"
            f"📄 Паспорт: {passport}\n"
            f"🆕 Категория: {category}\n"
            f"💰 Цена: {price}\n"          # ✅ добавили
            f"🔑 Серийный номер: {serial}\n"
            f"✔️ Проверка: {checked}"
        )


def load_df():
    if not os.path.exists(FILE_PATH):
        raise FileNotFoundError(
            f"Файл {FILE_PATH} не найден. Пришли его боту в Telegram как .xlsx"
        )

    df = pd.read_excel(FILE_PATH)
    df.columns = [str(c).strip() for c in df.columns]

    if not REQUIRED_COLUMNS.issubset(set(df.columns)):
        missing = sorted(list(REQUIRED_COLUMNS - set(df.columns)))
        raise ValueError("В Excel не хватает колонок: " + ", ".join(missing))

    # готовим строковые поля
    df["PartNumber"] = df["PartNumber"].astype(str)
    df["_pn_norm"] = df["PartNumber"].apply(normalize_part_for_search)
    return df


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет 👋\n"
        "Напиши PartNumber (или часть номера) — я найду.\n"
        "Чтобы обновить базу — пришли Excel файлом (.xlsx) сюда в чат."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — старт\n"
        "/help — помощь\n\n"
        "1) Поиск: просто отправь PartNumber или часть\n"
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

    # скачиваем и заменяем warehouse.xlsx
    tg_file = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(FILE_PATH)

    # быстрая проверка что файл норм читается и колонки есть
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

    query_raw = text
    query_norm = normalize_part_for_search(query_raw)

    try:
        df = load_df()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")
        return

    # 1) Точное/частичное совпадение (по нормализованному номеру)
    exact = df[df["_pn_norm"].str.contains(query_norm, na=False)]

    # если есть — отдаем
    if not exact.empty:
        responses = [fmt_row(row) for _, row in exact.iterrows()]
        await update.message.reply_text("\n\n".join(responses[:20]))
        if len(responses) > 20:
            await update.message.reply_text("ℹ️ Нашла много совпадений, показала первые 20.")
        return

    # 2) Fuzzy поиск (похожее)
    pn_list = df["_pn_norm"].tolist()
    close = difflib.get_close_matches(query_norm, pn_list, n=8, cutoff=0.6)

    if close:
        fuzzy = df[df["_pn_norm"].isin(close)]
        responses = [fmt_row(row) for _, row in fuzzy.iterrows()]
        await update.message.reply_text(
            "🤔 Точного совпадения нет, но нашла похожие:\n\n" + "\n\n".join(responses)
        )
        return

    await update.message.reply_text("❓ Ничего не нашла по этому запросу")


def main():
    if not TOKEN:
        raise RuntimeError("TOKEN не задан. Добавь TOKEN в Railway Variables.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # ВАЖНО: сначала документы, потом текст
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":  # ✅ важно: именно так
    main()
