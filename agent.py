import os
import pandas as pd
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

# ========= НАСТРОЙКИ =========
TOKEN = os.getenv("TOKEN")
EXCEL_FILE = "warehouse.xlsx"

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
# ==============================


def normalize(v):
    if pd.isna(v):
        return ""
    return str(v).strip()


def to_yes(v):
    return normalize(v).lower() in {"yes", "y", "true", "1", "да", "ok", "checked"}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()
    if not query:
        return

    try:
        df = pd.read_excel(EXCEL_FILE)
        df.columns = [str(c).strip() for c in df.columns]

        if not REQUIRED_COLUMNS.issubset(df.columns):
            missing = REQUIRED_COLUMNS - set(df.columns)
            await update.message.reply_text(
                "❌ В Excel не хватает колонок:\n" + ", ".join(missing)
            )
            return

        df["PartNumber"] = df["PartNumber"].astype(str)
        matches = df[df["PartNumber"].str.lower().str.contains(query.lower(), na=False)]

        if matches.empty:
            await update.message.reply_text("❌ Ничего не найдено")
            return

        replies = []

        for _, row in matches.iterrows():
            qty = int(float(row["Quantity"])) if not pd.isna(row["Quantity"]) else 0

            replies.append(
                f"{'✅' if qty > 0 else '❌'} {normalize(row['PartNumber'])}\n"
                f"📦 Полка: {normalize(row['Shelf'])}, ячейка: {normalize(row['Location'])}\n"
                f"🔢 Количество: {qty}\n"
                f"📄 Паспорт: {'есть' if to_yes(row['Passport']) else 'нет'}\n"
                f"🆕 Категория: {'новая' if normalize(row['Category']).lower() == 'new' else 'старая'}\n"
                f"🔑 Серийный номер: {normalize(row['SerialNumber']) or '—'}\n"
                f"✔️ Проверка: {'проверена' if to_yes(row['Check']) else 'не проверена'}"
            )

        await update.message.reply_text("\n\n".join(replies))

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


def main():
    if not TOKEN:
        raise ValueError("TOKEN is not set")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
