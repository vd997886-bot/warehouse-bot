import asyncio
import difflib
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
FILE_PATH = Path(os.getenv("WAREHOUSE_FILE", "warehouse.xlsx"))

REQUIRED_COLUMNS = [
    "PartNumber",
    "Quantity",
    "Shelf",
    "Location",
    "Passport",
    "Category",
    "SerialNumber",
    "Check",
]

OPTIONAL_COLUMNS = [
    "Price",
    "PhotoID",
    "SoldTo",
    "SoldDate",
    "Notes",
]

excel_lock = asyncio.Lock()

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔍 Найти запчасть", "🗑 Удалить запчасть"],
        ["📥 Скачать Excel", "↩️ Отменить удаление"],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def authorized(user_id: int) -> bool:
    return not ADMIN_ID or str(user_id) == str(ADMIN_ID)


def safe_str(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def normalize_part(value) -> str:
    text = safe_str(value).upper()
    return re.sub(r"[\s\-_./\\]+", "", text)


def qty_number(value) -> float:
    try:
        return float(safe_str(value).replace(",", "."))
    except Exception:
        return 0.0


def translate(value, field: str) -> str:
    text = safe_str(value).lower()

    if field == "passport":
        if text in {"yes", "y", "true", "1"}:
            return "есть"
        if text in {"no", "n", "false", "0"}:
            return "нет"

    if field == "check":
        if text in {"yes", "y", "true", "1"}:
            return "проверена"
        if text in {"no", "n", "false", "0"}:
            return "не проверена"

    if field == "category":
        return {
            "new": "новая",
            "used": "б/у",
            "serviceable": "исправная",
            "overhauled": "после ремонта",
        }.get(text, safe_str(value))

    return safe_str(value)


def load_dataframe() -> pd.DataFrame:
    if not FILE_PATH.exists():
        raise FileNotFoundError(f"Файл {FILE_PATH.name} не найден")

    df = pd.read_excel(FILE_PATH)
    df.columns = [safe_str(column) for column in df.columns]

    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError("В Excel не хватает обязательных колонок: " + ", ".join(missing))

    for column in OPTIONAL_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    df["PartNumber"] = df["PartNumber"].fillna("").astype(str)
    df["_pn_norm"] = df["PartNumber"].map(normalize_part)
    return df


def workbook_headers(workbook) -> dict[str, int]:
    sheet = workbook.active
    return {
        safe_str(cell.value): index
        for index, cell in enumerate(sheet[1], start=1)
        if safe_str(cell.value)
    }


def create_backup(prefix: str) -> Path:
    if not FILE_PATH.exists():
        raise FileNotFoundError(f"Файл {FILE_PATH.name} не найден")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = FILE_PATH.with_name(f"warehouse_{prefix}_{timestamp}.xlsx")
    shutil.copy2(FILE_PATH, backup)
    return backup


def parse_parts(text: str) -> list[str]:
    text = re.sub(r"^/delete(?:@\w+)?", "", safe_str(text), flags=re.I).strip()
    if not text:
        return []

    result: list[str] = []
    seen: set[str] = set()

    for item in re.split(r"[,;\n]+", text):
        item = item.strip()
        normalized = normalize_part(item)
        if item and normalized and normalized not in seen:
            seen.add(normalized)
            result.append(item)

    return result


def find_rows(parts: list[str]) -> tuple[list[dict], list[str]]:
    workbook = load_workbook(FILE_PATH, data_only=False)
    sheet = workbook.active
    headers = workbook_headers(workbook)

    part_col = headers.get("PartNumber")
    if not part_col:
        workbook.close()
        raise ValueError("В Excel нет колонки PartNumber")

    requested = {normalize_part(part): part for part in parts}
    found_norm: set[str] = set()
    rows: list[dict] = []

    def value(row: int, column_name: str):
        column = headers.get(column_name)
        return sheet.cell(row=row, column=column).value if column else ""

    for row_number in range(2, sheet.max_row + 1):
        part_value = value(row_number, "PartNumber")
        normalized = normalize_part(part_value)
        if normalized not in requested:
            continue

        found_norm.add(normalized)
        rows.append(
            {
                "row": row_number,
                "part_number": safe_str(part_value),
                "normalized": normalized,
                "serial_number": safe_str(value(row_number, "SerialNumber")),
                "quantity": safe_str(value(row_number, "Quantity")),
                "shelf": safe_str(value(row_number, "Shelf")),
                "location": safe_str(value(row_number, "Location")),
            }
        )

    workbook.close()
    not_found = [original for normalized, original in requested.items() if normalized not in found_norm]
    return rows, not_found


def verify_rows(rows: list[dict]) -> bool:
    workbook = load_workbook(FILE_PATH, data_only=False)
    sheet = workbook.active
    headers = workbook_headers(workbook)
    part_col = headers.get("PartNumber")

    if not part_col:
        workbook.close()
        return False

    for item in rows:
        row_number = item["row"]
        if row_number < 2 or row_number > sheet.max_row:
            workbook.close()
            return False

        current = sheet.cell(row=row_number, column=part_col).value
        if normalize_part(current) != item["normalized"]:
            workbook.close()
            return False

    workbook.close()
    return True


def delete_rows(rows: list[dict]) -> list[dict]:
    workbook = load_workbook(FILE_PATH)
    sheet = workbook.active
    deleted: list[dict] = []

    for item in sorted(rows, key=lambda row: row["row"], reverse=True):
        row_number = item["row"]
        if 2 <= row_number <= sheet.max_row:
            sheet.delete_rows(row_number, 1)
            deleted.append(item)

    workbook.save(FILE_PATH)
    workbook.close()
    return sorted(deleted, key=lambda row: row["row"])


def format_row(row) -> str:
    part = safe_str(row.get("PartNumber"))
    quantity = safe_str(row.get("Quantity"))
    shelf = safe_str(row.get("Shelf"))
    location = safe_str(row.get("Location"))
    passport = translate(row.get("Passport"), "passport")
    category = translate(row.get("Category"), "category")
    serial = safe_str(row.get("SerialNumber")) or "—"
    check = translate(row.get("Check"), "check")
    price = safe_str(row.get("Price")) or "—"

    status = "❌ ПРОДАНО" if qty_number(row.get("Quantity")) <= 0 else f"✅ {part} есть в наличии"
    if status == "❌ ПРОДАНО":
        status = f"❌ ПРОДАНО\n📦 {part}"

    text = (
        f"{status}\n"
        f"📍 Полка: {shelf}, ячейка: {location}\n"
        f"🔢 Количество: {quantity}\n"
        f"📄 Паспорт: {passport}\n"
        f"🆕 Категория: {category}\n"
        f"💰 Цена: {price}\n"
        f"🔑 Серийный номер: {serial}\n"
        f"✔ Проверка: {check}"
    )

    sold_to = safe_str(row.get("SoldTo"))
    sold_date = safe_str(row.get("SoldDate"))
    notes = safe_str(row.get("Notes"))

    if sold_to:
        text += f"\n👤 Кому продано: {sold_to}"
    if sold_date:
        text += f"\n📅 Дата продажи: {sold_date.split(' ')[0]}"
    if notes:
        text += f"\n📝 Заметка: {notes}"

    return text


def delete_preview(found: list[dict], not_found: list[str]) -> str:
    lines = ["⚠️ Будут полностью удалены:", ""]

    for item in found[:40]:
        line = f"📦 {item['part_number']}"
        if item["serial_number"]:
            line += f" | S/N: {item['serial_number']}"
        if item["quantity"]:
            line += f" | Qty: {item['quantity']}"
        lines.append(line)

    if len(found) > 40:
        lines += ["", f"…и ещё {len(found) - 40} строк."]

    if not_found:
        lines += ["", "❌ Не найдены:"]
        lines += [f"• {part}" for part in not_found[:30]]

    lines += ["", f"Всего строк будет удалено: {len(found)}"]
    return "\n".join(lines)


async def send_excel(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str) -> None:
    if not FILE_PATH.exists():
        await context.bot.send_message(chat_id=chat_id, text="❌ Excel-файл не найден")
        return

    with FILE_PATH.open("rb") as file:
        await context.bot.send_document(
            chat_id=chat_id,
            document=file,
            filename="warehouse_actual.xlsx",
            caption=caption,
            reply_markup=MAIN_KEYBOARD,
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "Привет! 👋 Выбери действие кнопкой внизу.",
        reply_markup=MAIN_KEYBOARD,
    )


async def begin_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "search"
    await update.message.reply_text(
        "🔍 Отправь PartNumber или часть номера.",
        reply_markup=MAIN_KEYBOARD,
    )


async def begin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа", reply_markup=MAIN_KEYBOARD)
        return

    context.user_data["mode"] = "delete"
    await update.message.reply_text(
        "🗑 Отправь PartNumber. Несколько номеров можно через запятую или с новой строки.",
        reply_markup=MAIN_KEYBOARD,
    )


async def search_part(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    query = normalize_part(text)
    if not query:
        await update.message.reply_text("❓ Напиши PartNumber", reply_markup=MAIN_KEYBOARD)
        return

    try:
        df = load_dataframe()
    except Exception as error:
        await update.message.reply_text(f"⚠️ Ошибка: {error}", reply_markup=MAIN_KEYBOARD)
        return

    exact = df[df["_pn_norm"] == query]
    matches = exact

    if matches.empty:
        matches = df[df["_pn_norm"].str.contains(query, na=False, regex=False)]

    if matches.empty:
        close = difflib.get_close_matches(query, df["_pn_norm"].tolist(), n=10, cutoff=0.75)
        if close:
            matches = df[df["_pn_norm"].isin(close)]

    if matches.empty:
        await update.message.reply_text("❓ Ничего не найдено", reply_markup=MAIN_KEYBOARD)
        return

    for _, row in matches.head(10).iterrows():
        photo_id = safe_str(row.get("PhotoID"))
        caption = format_row(row)

        if photo_id:
            try:
                await update.message.reply_photo(photo=photo_id, caption=caption, reply_markup=MAIN_KEYBOARD)
                continue
            except Exception:
                pass

        await update.message.reply_text(caption, reply_markup=MAIN_KEYBOARD)

    if len(matches) > 10:
        await update.message.reply_text(
            f"ℹ️ Всего найдено {len(matches)}. Показаны первые 10.",
            reply_markup=MAIN_KEYBOARD,
        )


async def prepare_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    parts = parse_parts(text)
    if not parts:
        await update.message.reply_text("❌ Не удалось прочитать PartNumber", reply_markup=MAIN_KEYBOARD)
        return

    try:
        async with excel_lock:
            found, not_found = find_rows(parts)
    except Exception as error:
        await update.message.reply_text(f"⚠️ Ошибка: {error}", reply_markup=MAIN_KEYBOARD)
        return

    if not found:
        await update.message.reply_text("❌ Ничего не найдено для удаления", reply_markup=MAIN_KEYBOARD)
        return

    context.user_data["pending_delete"] = found

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Удалить", callback_data="confirm_delete"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_delete"),
        ]]
    )
    await update.message.reply_text(delete_preview(found, not_found), reply_markup=keyboard)


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not authorized(query.from_user.id):
        await query.edit_message_text("⛔ Нет доступа")
        return

    if query.data == "cancel_delete":
        context.user_data.pop("pending_delete", None)
        context.user_data["mode"] = None
        await query.edit_message_text("❌ Удаление отменено")
        return

    pending = context.user_data.get("pending_delete")
    if not pending:
        await query.edit_message_text("⚠️ Список удаления устарел. Начни заново.")
        return

    try:
        async with excel_lock:
            if not verify_rows(pending):
                raise RuntimeError("Excel изменился. Начни удаление заново.")

            backup = create_backup("before_delete")
            deleted = delete_rows(pending)
            context.application.bot_data["last_backup"] = str(backup)
    except Exception as error:
        await query.edit_message_text(f"⚠️ Ошибка удаления: {error}")
        return

    context.user_data.clear()
    await query.edit_message_text(f"✅ Удалено строк: {len(deleted)}")
    await send_excel(
        context,
        query.message.chat_id,
        "📥 Обновлённый Excel. Удалённые позиции уже убраны.",
    )


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа", reply_markup=MAIN_KEYBOARD)
        return

    backup = context.application.bot_data.get("last_backup")
    if not backup or not Path(backup).exists():
        await update.message.reply_text("❌ Нечего отменять", reply_markup=MAIN_KEYBOARD)
        return

    try:
        async with excel_lock:
            create_backup("before_undo")
            shutil.copy2(backup, FILE_PATH)
            context.application.bot_data.pop("last_backup", None)
    except Exception as error:
        await update.message.reply_text(f"⚠️ Ошибка восстановления: {error}", reply_markup=MAIN_KEYBOARD)
        return

    await update.message.reply_text("✅ Последнее удаление отменено", reply_markup=MAIN_KEYBOARD)
    await send_excel(context, update.effective_chat.id, "📥 Восстановленный Excel")


async def download_excel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа", reply_markup=MAIN_KEYBOARD)
        return

    async with excel_lock:
        await send_excel(context, update.effective_chat.id, "📥 Актуальный склад")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа", reply_markup=MAIN_KEYBOARD)
        return

    document = update.message.document
    if not document or not (document.file_name or "").lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Пришли файл .xlsx", reply_markup=MAIN_KEYBOARD)
        return

    temp = FILE_PATH.with_name("warehouse_upload_temp.xlsx")

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(temp)

        test_df = pd.read_excel(temp)
        test_df.columns = [safe_str(column) for column in test_df.columns]
        missing = [column for column in REQUIRED_COLUMNS if column not in test_df.columns]
        if missing:
            raise ValueError("Не хватает колонок: " + ", ".join(missing))

        async with excel_lock:
            if FILE_PATH.exists():
                create_backup("before_upload")
            shutil.move(temp, FILE_PATH)
            load_dataframe()
    except Exception as error:
        temp.unlink(missing_ok=True)
        await update.message.reply_text(f"⚠️ Не удалось загрузить Excel: {error}", reply_markup=MAIN_KEYBOARD)
        return

    context.user_data.clear()
    await update.message.reply_text("✅ Таблица обновлена", reply_markup=MAIN_KEYBOARD)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    photo_id = update.message.photo[-1].file_id
    await update.message.reply_text(
        f"PhotoID:\n{photo_id}",
        reply_markup=MAIN_KEYBOARD,
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = re.sub(r"^/delete(?:@\w+)?", "", update.message.text or "", flags=re.I).strip()
    if text:
        await prepare_delete(update, context, text)
    else:
        await begin_delete(update, context)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = safe_str(update.message.text)

    if text == "🔍 Найти запчасть":
        await begin_search(update, context)
    elif text == "🗑 Удалить запчасть":
        await begin_delete(update, context)
    elif text == "📥 Скачать Excel":
        context.user_data["mode"] = None
        await download_excel(update, context)
    elif text == "↩️ Отменить удаление":
        await undo(update, context)
    elif text == "❌ Отмена":
        context.user_data.clear()
        await update.message.reply_text("❌ Действие отменено", reply_markup=MAIN_KEYBOARD)
    elif context.user_data.get("mode") == "delete":
        await prepare_delete(update, context, text)
    else:
        await search_part(update, context, text)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Добавь TOKEN в Railway Variables")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CallbackQueryHandler(delete_callback, pattern=r"^(confirm_delete|cancel_delete)$"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 Warehouse bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
