import os
import re
import pandas as pd

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

# ========== SETTINGS ==========
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


def normalize_text(v) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def to_yes(v) -> bool:
    v = normalize_text(v).lower()
    return v in {"yes", "y", "true", "1", "да", "ok", "checked"}


def norm_key(s: str) -> str:
    """
    Нормализация для "похожего" поиска:
    - upper
    - убрать пробелы, дефисы, слэши, точки, запятые и т.п.
    """
    s = normalize_text(s).upper()
    s = re.sub(r"[\s\-\_/\\\.,;:]+", "", s)
    return s


def make_fuzzy_regex(query: str) -> re.Pattern:
    """
    Делает regex, который позволяет искать с любыми разделителями между символами/группами.
    Пример: "PH6002CEP" найдёт "PH-600 2 Cep"
    """
    q = norm_key(query)
    if not q:
        return re.compile(r"$^")  # никогда не матчится

    # между символами разрешим любые разделители/пробелы
    # + делаем “мягко”, но без дикого тормоза
    parts = list(q)
    pattern = r".*".join(map(re.escape, parts))
    return re.compile(pattern, re.IGNORECASE)


def safe_int(v) -> int:
    try:
        if pd.isna(v):
            return 0
        return int(float(v))
    except Exception:
        return 0


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_raw = (update.message.text or "").strip()
    if not query_raw:
        return

    try:
        df = pd.read_excel(EXCEL_FILE)
        df.columns = [str(c).strip() for c in df.columns]

        if not REQUIRED_COLUMNS.issubset(set(df.columns)):
            missing = sorted(list(REQUIRED_COLUMNS - set(df.columns)))
            await update.message.reply_text(
                "❌ Ошибка: в Excel не хватает колонок:\n" + ", ".join(missing)
            )
            return

        # Приводим PartNumber к строке
        df["PartNumber"] = df["PartNumber"].astype(str)

        # 1) Обычный contains (быстрый)
        contains_matches = df[df["PartNumber"].str.contains(query_raw, case=False, na=False)]

        # 2) "Похожий" поиск (нормализация)
        qk = norm_key(query_raw)
        df["_pn_norm"] = df["PartNumber"].map(norm_key)
        fuzzy_matches = df[df["_pn_norm"].str.contains(qk, na=False)] if qk else df.iloc[0:0]

        # 3) Ещё более мягко: regex по символам (если вообще ничего не нашли)
        if contains_matches.empty and fuzzy_matches.empty:
            rgx = make_fuzzy_regex(query_raw)
            regex_matches = df[df["PartNumber"].str.contains(rgx, na=False)]
        else:
            regex_matches = df.iloc[0:0]

        # Собираем всё и убираем дубликаты
        matches = pd.concat([contains_matches, fuzzy_matches, regex_matches]).drop_duplicates()

        if matches.empty:
            await update.message.reply_text("❓ Ничего похожего не нашла в таблице")
            return

        # Ограничим ответ, чтобы телега не взорвалась, если совпадений много
        matches = matches.head(10)

        responses = []
        for _, row in matches.iterrows():
            part = normalize_text(row["PartNumber"])
            qty = safe_int(row["Quantity"])
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
    if not TOKEN:
        raise ValueError("TOKEN is not set. Add TOKEN in Railway Variables.")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🤖 Avacs Stock Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
