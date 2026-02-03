import os
import pandas as pd

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# ===== НАСТРОЙКИ =====
TOKEN = os.getenv("TOKEN")  # токен ТОЛЬКО через Railway Variables
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
# ====================


def normalize(v) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def is_yes(v) -> bool:
    return normalize(v).lower() in {"yes", "y", "true", "1", "да", "ok", "checked"}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip().lower()
    if not query:
        return

    try:
        df = pd.read_excel(EXCEL_FILE)
        df.columns = [str(c).strip() for c in df.columns]

        # проверка колонок
        if not REQUIRED_COLUMNS.issubset(df.columns):
            missing = REQUIRED_COLUMNS - set(df.columns)
            await update.message.reply_text(
                "❌ В Excel не хватает колонок:\n" + ", ".join(missing)
            )
            return

        df["PartNumber"] = df["PartNumber"].astype(str)

        # 🔍 ПОИСК ПОХОЖИХ (contains)
        matches = df[df["PartNumber"].str.lower().str.contains(query, na=False)]

        if matches.empty:
            await update.message.reply_text("❓ Такой запчасти нет")
            return

        answers = []

        for _, row in matches.iterrows():
            part = normalize(row["PartNumber"])

            try:
                qty = int(float(row["Quantity"]))
            except Exception:
                qty = 0

            shelf = normalize(row["Shelf"])
            location = normalize(row["Location"])
            passport = "есть" if is_yes(row["Passport"]) else "нет"
            category = "новая" if normalize(row["Category"]).lower() == "new" else "старая"
            serial = normalize(row["SerialNumber"]) or "—"
            checked = "проверена" if is_yes(row["Check"]) else "не проверена"

            if qty > 0:
                answers.append(
                    f"✅ {part}\n"
                    f"📦 Полка: {shelf}, ячейка: {location}\n"
                    f"🔢 Количество: {qty}\n"
                    f"📄 Паспорт: {passport}\n"
                    f"🆕 Категория: {category}\n"
                    f"🔑 Серийный номер: {serial}\n"
                    f"✔️ Проверка: {checked}"
                )
            else:
                answers.append(
                    f"❌ {part} — нет в наличии\n"
                    f"📄 Паспорт: {passport}\n"
                    f"🆕 Категория: {category}\n"
                    f"🔑 Серийный номер: {serial}\n"
                    f"✔️ Проверка: {checked}"
                )

        await update.message.reply_text("\n\n".join(answers))

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


def main():
    if not TOKEN:
        raise RuntimeError("TOKEN не найден в переменных окружения")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Warehouse bot запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
