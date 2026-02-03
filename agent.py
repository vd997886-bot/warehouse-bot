import os
import re
import pandas as pd

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ========== НАСТРОЙКИ ==========
TOKEN = os.getenv("TOKEN")  # добавь в Railway Variables: TOKEN=...
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
}
# ===============================


def normalize_text(v) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def to_yes(v) -> bool:
    v = normalize_text(v).lower()
    return v in {"yes", "y", "true", "1", "да", "ok", "checked"}


def normalize_query(s: str) -> str:
    """Нормализация для 'похожего' поиска: убираем пробелы/дефисы/слэши, приводим к lower."""
    s = (s or "").strip().lower()
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[-_/\\]+", "", s)
    return s


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Напиши part number (или часть), и я найду.\n"
        "Чтобы обновить таблицу: отправь .xlsx файлом в этот чат (или /update)."
    )


async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_file"] = True
    await update.message.reply_text("Ок, пришли Excel (.xlsx) файлом сюда — я обновлю warehouse.xlsx ✅")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return

    filename = (doc.file_name or "").lower()

    # принимаем только xlsx
    if not filename.endswith(".xlsx"):
        await update.message.reply_text("Пришли именно Excel файл .xlsx")
        return

    # если хочешь строго только по /update, раскомментируй:
    # if not context.user_data.get("awaiting_file"):
    #     await update.message.reply_text("Если хочешь обновить таблицу — напиши /update и затем пришли файл.")
    #     return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        tmp_path = "warehouse_upload.xlsx"
        await tg_file.download_to_drive(custom_path=tmp_path)

        # проверим что файл читается и колонки на месте
        df = pd.read_excel(tmp_path)
        df.columns = [str(c).strip() for c in df.columns]

        if not REQUIRED_COLUMNS.issubset(set(df.columns)):
            missing = sorted(list(REQUIRED_COLUMNS - set(df.columns)))
            await update.message.reply_text(
                "❌ В файле не хватает колонок:\n" + ", ".join(missing) + "\n\nНичего не обновила."
            )
            os.remove(tmp_path)
            return

        # заменить основной файл
        if os.path.exists(FILE_PATH):
            os.remove(FILE_PATH)
        os.rename(tmp_path, FILE_PATH)

        context.user_data["awaiting_file"] = False
        await update.message.reply_text(f"✅ Таблица обновлена! Строк: {len(df)}")

    except Exception as e:
        await update.message.reply_text(f"⚠️ Не смогла обновить файл: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()
    if not query:
        return

    if not os.path.exists(FILE_PATH):
        await update.message.reply_text("⚠️ Таблица не найдена на сервере. Пришли warehouse.xlsx файлом сюда.")
        return

    try:
        df = pd.read_excel(FILE_PATH)
        df.columns = [str(c).strip() for c in df.columns]

        if not REQUIRED_COLUMNS.issubset(set(df.columns)):
            missing = sorted(list(REQUIRED_COLUMNS - set(df.columns)))
            await update.message.reply_text("❌ В Excel не хватает колонок:\n" + ", ".join(missing))
            return

        # подготовка к "похожему" поиску
        df["PartNumber"] = df["PartNumber"].astype(str)
        df["_pn_norm"] = df["PartNumber"].map(normalize_query)

        q_norm = normalize_query(query)

        # 1) строгий contains по нормализованному
        matches = df[df["_pn_norm"].str.contains(q_norm, na=False)]

        # 2) если ничего — попробуем обычный contains (на случай если запрос с дефисами)
        if matches.empty:
            matches = df[df["PartNumber"].str.lower().str.contains(query.lower(), na=False)]

        if matches.empty:
            await update.message.reply_text("❓ Не нашла такую запчасть в таблице")
            return

        responses = []
        for _, row in matches.head(10).iterrows():  # ограничим, чтобы не спамило
            part = normalize_text(row["PartNumber"])

            try:
                qty = int(float(row["Quantity"])) if not pd.isna(row["Quantity"]) else 0
            except Exception:
                qty = 0

            shelf = normalize_text(row["Shelf"])
            location = normalize_text(row["Location"])

            passport = "есть" if to_yes(row["Passport"]) else "нет"

            cat_raw = normalize_text(row["Category"]).lower()
            category = "новая" if cat_raw == "new" else ("старая" if cat_raw else "—")

            serial = normalize_text(row["SerialNumber"]) or "—"

            checked = "проверена" if to_yes(row["Check"]) else "не проверена"

            if qty > 0:
                responses.append(
                    f"✅ {part} есть в наличии\n"
                    f"📦 Полка: {shelf}, ячейка: {location}\n"
                    f"🔢 Количество: {qty}\n"
                    f"📄 Паспорт: {passport}\n"
                    f"🆕 Категория: {category}\n"
                    f"🔑 Серийный номер: {serial}\n"
                    f"✔️ Проверка: {checked}"
                )
            else:
                responses.append(
                    f"❌ {part} нет в наличии\n"
                    f"📄 Паспорт: {passport}\n"
                    f"🆕 Категория: {category}\n"
                    f"🔑 Серийный номер: {serial}\n"
                    f"✔️ Проверка: {checked}"
                )

        extra = ""
        if len(matches) > 10:
            extra = f"\n\nℹ️ Нашла {len(matches)} совпадений, показала первые 10."

        await update.message.reply_text("\n\n".join(responses) + extra)

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


def main():
    if not TOKEN:
        raise RuntimeError("TOKEN is missing. Add TOKEN in Railway Variables.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("update", update_cmd))

    # документ (xlsx)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # текстовый поиск
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
