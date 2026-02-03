import os
import re
import pandas as pd

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters


# ================= НАСТРОЙКИ =================
TOKEN = os.getenv("TOKEN")          # Токен ТОЛЬКО из Railway Variables
FILE_PATH = "warehouse.xlsx"        # Excel лежит рядом с agent.py

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
# =============================================


def normalize_text(v) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def to_yes(v: str) -> bool:
    v = normalize_text(v).lower()
    return v in {"yes", "y", "true", "1", "да", "ok", "checked"}


def normalize_pn(v: str) -> str:
    if pd.isna(v):
        return ""
    v = str(v).lower()
    v = re.sub(r"[^a-z0-9]", "", v)  # убираем дефисы, пробелы, символы
    return v


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_raw = (update.message.text or "").strip()
    if not query_raw:
        return

    try:
        # Читаем Excel
        df = pd.read_excel(FILE_PATH, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]

        # Проверка колонок
        if not REQUIRED_COLUMNS.issubset(set(df.columns)):
            missing = sorted(list(REQUIRED_COLUMNS - set(df.columns)))
            await update.message.reply_text(
                "❌ В Excel не хватает колонок:\n" + ", ".join(missing)
            )
            return

        # Подготовка для умного поиска
        df["PartNumber"] = df["PartNumber"].astype(str)
        df["pn_norm"] = df["PartNumber"].apply(normalize_pn)

        query_norm = normalize_pn(query_raw)

        # УМНЫЙ ПОИСК (похожие номера)
        matches = df[df["pn_norm"].str.contains(query_norm, na=False)]

        # Если совсем ничего — пробуем по частям
        if matches.empty:
            parts = [p for p in re.split(r"[-\s]", query_raw) if p]
            if parts:
                mask = False
                for p in parts:
                    mask = mask | df["PartNumber"].str.contains(p, case=False, na=False)
                matches = df[mask]

        if matches.empty:
            await update.message.reply_text("❓ Такой запчасти нет в таблице")
            return

        responses = []

        for _, row in matches.iterrows():
            part = normalize_text(row["PartNumber"])

            # Quantity
            try:
                qty = int(float(row["Quantity"])) if not pd.isna(row["Quantity"]) else 0
            except Exception:
                qty = 0

            shelf = normalize_text(row["Shelf"])
            location = normalize_text(row["Location"])

            passport = "есть" if to_yes(row["Passport"]) else "нет"

            cat_raw = normalize_text(row["Category"]).lower()
            category = "новая" if cat_raw == "new" else "старая"

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

        await update.message.reply_text("\n\n".join(responses))

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Avacs Stock Bot запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
