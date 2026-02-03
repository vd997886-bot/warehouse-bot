import os
import re
import difflib
from typing import Optional, List

import pandas as pd
from telegram import Update, Document
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== SETTINGS ==================
TOKEN = os.getenv("TOKEN")  # Railway Variable
EXCEL_FILE = os.getenv("EXCEL_FILE", "warehouse.xlsx")  # имя файла на сервере

# Ограничим загрузку файла только тебе (по желанию)
# ADMIN_IDS="123456789,987654321"
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}
# ==============================================

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

_df_cache: Optional[pd.DataFrame] = None


def normalize_text(v) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def to_yes(v) -> bool:
    v = normalize_text(v).lower()
    return v in {"yes", "y", "true", "1", "да", "ok", "checked", "есть"}


def norm_key(s: str) -> str:
    """
    Нормализация для "похожего" поиска:
    убираем пробелы/дефисы/слеши, приводим к нижнему регистру.
    """
    s = normalize_text(s).lower()
    s = re.sub(r"[^a-z0-9а-я]+", "", s)  # оставляем буквы/цифры
    return s


def load_df(force: bool = False) -> pd.DataFrame:
    global _df_cache
    if _df_cache is not None and not force:
        return _df_cache

    df = pd.read_excel(EXCEL_FILE)
    df.columns = [str(c).strip() for c in df.columns]

    if not REQUIRED_COLUMNS.issubset(set(df.columns)):
        missing = sorted(list(REQUIRED_COLUMNS - set(df.columns)))
        raise ValueError("В Excel не хватает колонок: " + ", ".join(missing))

    df["PartNumber"] = df["PartNumber"].astype(str)
    df["_pn_norm"] = df["PartNumber"].apply(norm_key)

    _df_cache = df
    return df


def format_row(row: pd.Series) -> str:
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
        return (
            f"✅ {part} есть в наличии\n"
            f"📦 Полка: {shelf}, ячейка: {location}\n"
            f"🔢 Количество: {qty}\n"
            f"📄 Паспорт: {passport}\n"
            f"🆕 Категория: {category}\n"
            f"🔑 Серийный номер: {serial}\n"
            f"✔️ Проверка: {checked}"
        )
    else:
        return (
            f"❌ {part} нет в наличии\n"
            f"📄 Паспорт: {passport}\n"
            f"🆕 Категория: {category}\n"
            f"🔑 Серийный номер: {serial}\n"
            f"✔️ Проверка: {checked}"
        )


def find_matches(df: pd.DataFrame, query: str) -> pd.DataFrame:
    q_raw = query.strip()
    q_norm = norm_key(q_raw)

    # 1) обычный contains по оригиналу
    m1 = df[df["PartNumber"].str.lower().str.contains(q_raw.lower(), na=False)]
    if not m1.empty:
        return m1

    # 2) contains по нормализованному (без дефисов/пробелов)
    if q_norm:
        m2 = df[df["_pn_norm"].str.contains(q_norm, na=False)]
        if not m2.empty:
            return m2

    return df.iloc[0:0]  # empty


def suggest_similar(df: pd.DataFrame, query: str, limit: int = 8) -> List[str]:
    q_norm = norm_key(query)
    if not q_norm:
        return []

    # берем лучшие похожие по difflib
    pool = df["_pn_norm"].dropna().astype(str).unique().tolist()
    close = difflib.get_close_matches(q_norm, pool, n=limit, cutoff=0.6)
    if not close:
        return []

    # возвращаем оригинальные PartNumber для этих нормализованных
    res = []
    for c in close:
        originals = df.loc[df["_pn_norm"] == c, "PartNumber"].astype(str).unique().tolist()
        for o in originals:
            if o not in res:
                res.append(o)
            if len(res) >= limit:
                break
        if len(res) >= limit:
            break
    return res


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()
    if not query:
        return

    try:
        df = load_df(force=False)
        matches = find_matches(df, query)

        if matches.empty:
            sim = suggest_similar(df, query)
            if sim:
                await update.message.reply_text(
                    "❓ Точного совпадения нет.\n"
                    "Вот похожие варианты:\n• " + "\n• ".join(sim)
                )
            else:
                await update.message.reply_text("❓ Такой запчасти нет в таблице")
            return

        responses = [format_row(row) for _, row in matches.iterrows()]
        await update.message.reply_text("\n\n".join(responses))

    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def handle_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # безопасность: принимать файл только от админа (если ADMIN_IDS задан)
    user_id = update.effective_user.id if update.effective_user else None
    if ADMIN_IDS and (user_id not in ADMIN_IDS):
        await update.message.reply_text("⛔️ У тебя нет доступа к обновлению файла.")
        return

    doc: Document = update.message.document
    if not doc:
        return

    name = (doc.file_name or "").lower()
    if not name.endswith(".xlsx"):
        await update.message.reply_text("Пожалуйста отправь файл .xlsx")
        return

    try:
        file = await context.bot.get_file(doc.file_id)

        tmp_path = EXCEL_FILE + ".tmp"
        await file.download_to_drive(custom_path=tmp_path)

        # проверим, что файл читается и есть нужные колонки
        test_df = pd.read_excel(tmp_path)
        test_df.columns = [str(c).strip() for c in test_df.columns]
        if not REQUIRED_COLUMNS.issubset(set(test_df.columns)):
            missing = sorted(list(REQUIRED_COLUMNS - set(test_df.columns)))
            os.remove(tmp_path)
            await update.message.reply_text("❌ В файле не хватает колонок:\n" + ", ".join(missing))
            return

        # заменить основной файл
        os.replace(tmp_path, EXCEL_FILE)

        # сброс кэша чтобы бот читал новый файл
        load_df(force=True)

        await update.message.reply_text("✅ Файл обновлён! Теперь поиск работает по новой таблице.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Не смог обновить файл: {e}")


def main():
    if not TOKEN:
        raise RuntimeError("TOKEN не задан. Добавь TOKEN в Railway Variables.")

    app = ApplicationBuilder().token(TOKEN).build()

    # Приём Excel
    app.add_handler(MessageHandler(filters.Document.ALL, handle_excel))

    # Поиск по тексту
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
