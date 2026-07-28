import asyncio
import difflib
import os
import re
import shutil
from datetime import datetime

import pandas as pd
from openpyxl import load_workbook
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


TOKEN = os.getenv("TOKEN")
FILE_PATH = "warehouse.xlsx"

# Необязательно.
# Можно добавить в Railway переменную ADMIN_ID со своим Telegram ID.
# Тогда удалять строки и загружать новый Excel сможете только вы.
ADMIN_ID = os.getenv("ADMIN_ID")

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
    "SoldTo",
    "SoldDate",
    "Notes",
]

# Защищает Excel от одновременного изменения несколькими командами.
excel_lock = asyncio.Lock()


def is_authorized(user_id: int) -> bool:
    """
    Если ADMIN_ID не задан — бот разрешает изменения всем.
    Если ADMIN_ID задан — изменения доступны только этому пользователю.
    """
    if not ADMIN_ID:
        return True

    return str(user_id) == str(ADMIN_ID)


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


def clean_date(value) -> str:
    if pd.isna(value):
        return ""

    try:
        dt = pd.to_datetime(value)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(value).split(" ")[0]


def qty_to_number(qty_value) -> float:
    qty = safe_str(qty_value).replace(",", ".")

    try:
        return float(qty)
    except Exception:
        return 0.0


def load_df() -> pd.DataFrame:
    if not os.path.exists(FILE_PATH):
        raise FileNotFoundError(
            f"Файл {FILE_PATH} не найден. "
            "Пришли .xlsx файлом в бота, чтобы загрузить таблицу."
        )

    df = pd.read_excel(FILE_PATH)
    df.columns = [str(col).strip() for col in df.columns]

    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]

    if missing:
        raise ValueError(
            "В Excel не хватает колонок:\n" + ", ".join(missing)
        )

    df["PartNumber"] = df["PartNumber"].astype(str)
    df["_pn_norm"] = df["PartNumber"].apply(normalize_part_for_search)

    return df


def create_backup(prefix: str = "backup") -> str:
    """
    Создаёт резервную копию Excel рядом с warehouse.xlsx.
    """
    if not os.path.exists(FILE_PATH):
        raise FileNotFoundError(f"Файл {FILE_PATH} не найден.")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_path = f"warehouse_{prefix}_{timestamp}.xlsx"

    shutil.copy2(FILE_PATH, backup_path)
    return backup_path


def get_excel_headers(workbook) -> dict:
    """
    Возвращает словарь:
    название колонки -> номер колонки Excel.
    """
    sheet = workbook.active
    headers = {}

    for column_number, cell in enumerate(sheet[1], start=1):
        if cell.value is not None:
            headers[str(cell.value).strip()] = column_number

    return headers


def get_rows_preview(row_numbers: list[int]) -> tuple[list[dict], list[int]]:
    """
    Проверяет номера строк и возвращает информацию о найденных позициях.

    Номера — именно те, которые видны слева в Excel.
    Например, строка 2 — первая запчасть после заголовков.
    """
    if not os.path.exists(FILE_PATH):
        raise FileNotFoundError(f"Файл {FILE_PATH} не найден.")

    workbook = load_workbook(FILE_PATH, data_only=False)
    sheet = workbook.active
    headers = get_excel_headers(workbook)

    part_column = headers.get("PartNumber")
    serial_column = headers.get("SerialNumber")
    quantity_column = headers.get("Quantity")

    if not part_column:
        workbook.close()
        raise ValueError("В Excel не найдена колонка PartNumber.")

    found_rows = []
    invalid_rows = []

    for row_number in sorted(set(row_numbers)):
        if row_number < 2 or row_number > sheet.max_row:
            invalid_rows.append(row_number)
            continue

        part_number = sheet.cell(
            row=row_number,
            column=part_column,
        ).value

        serial_number = ""
        quantity = ""

        if serial_column:
            serial_number = sheet.cell(
                row=row_number,
                column=serial_column,
            ).value

        if quantity_column:
            quantity = sheet.cell(
                row=row_number,
                column=quantity_column,
            ).value

        found_rows.append(
            {
                "row": row_number,
                "part_number": (
                    str(part_number).strip()
                    if part_number is not None
                    else "Пустая строка"
                ),
                "serial_number": (
                    str(serial_number).strip()
                    if serial_number is not None
                    else ""
                ),
                "quantity": (
                    str(quantity).strip()
                    if quantity is not None
                    else ""
                ),
            }
        )

    workbook.close()
    return found_rows, invalid_rows


def delete_excel_rows(row_numbers: list[int]) -> list[dict]:
    """
    Полностью удаляет строки из Excel.

    Строки удаляются снизу вверх, поэтому номера не съезжают
    во время удаления.
    """
    if not os.path.exists(FILE_PATH):
        raise FileNotFoundError(f"Файл {FILE_PATH} не найден.")

    workbook = load_workbook(FILE_PATH)
    sheet = workbook.active
    headers = get_excel_headers(workbook)

    part_column = headers.get("PartNumber")
    serial_column = headers.get("SerialNumber")

    if not part_column:
        workbook.close()
        raise ValueError("В Excel не найдена колонка PartNumber.")

    deleted_items = []

    # Обязательно удаляем от самой нижней строки к верхней.
    for row_number in sorted(set(row_numbers), reverse=True):
        if row_number < 2 or row_number > sheet.max_row:
            continue

        part_number = sheet.cell(
            row=row_number,
            column=part_column,
        ).value

        serial_number = ""

        if serial_column:
            serial_number = sheet.cell(
                row=row_number,
                column=serial_column,
            ).value

        deleted_items.append(
            {
                "row": row_number,
                "part_number": (
                    str(part_number).strip()
                    if part_number is not None
                    else "Пустая строка"
                ),
                "serial_number": (
                    str(serial_number).strip()
                    if serial_number is not None
                    else ""
                ),
            }
        )

        sheet.delete_rows(row_number, 1)

    workbook.save(FILE_PATH)
    workbook.close()

    return sorted(deleted_items, key=lambda item: item["row"])


def format_delete_preview(items: list[dict], invalid_rows: list[int]) -> str:
    lines = [
        "⚠️ Будут полностью удалены следующие строки:",
        "",
    ]

    # Чтобы сообщение не стало слишком длинным,
    # показываем максимум первые 40 позиций.
    shown_items = items[:40]

    for item in shown_items:
        line = f'{item["row"]} — {item["part_number"]}'

        if item["serial_number"]:
            line += f' | S/N: {item["serial_number"]}'

        if item["quantity"]:
            line += f' | Qty: {item["quantity"]}'

        lines.append(line)

    if len(items) > 40:
        lines.append("")
        lines.append(
            f"…и ещё {len(items) - 40} позиций."
        )

    if invalid_rows:
        lines.append("")
        lines.append(
            "❌ Не существуют строки: "
            + ", ".join(map(str, invalid_rows))
        )

    lines.append("")
    lines.append(
        f"Всего будет удалено: {len(items)}"
    )
    lines.append("")
    lines.append(
        "После удаления эти запчасти исчезнут из Excel "
        "и больше не будут появляться в поиске."
    )

    return "\n".join(lines)


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

    sold_to = safe_str(row.get("SoldTo"))
    sold_date = clean_date(row.get("SoldDate"))
    notes = safe_str(row.get("Notes"))

    qty_num = qty_to_number(row.get("Quantity"))

    if qty_num <= 0:
        text = (
            f"❌ ПРОДАНО\n"
            f"📦 {part}\n"
            f"📍 Полка: {shelf}, ячейка: {location}\n"
            f"🔢 Количество: {qty}\n"
            f"📄 Паспорт: {passport}\n"
            f"🆕 Категория: {category}\n"
            f"💰 Цена: {price}\n"
            f"🔑 Серийный номер: {serial}\n"
            f"✔ Проверка: {check}"
        )

        if sold_to:
            text += f"\n👤 Кому продано: {sold_to}"

        if sold_date:
            text += f"\n📅 Дата продажи: {sold_date}"

        if notes:
            text += f"\n📝 Заметка: {notes}"

        return text

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


async def send_part_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    row,
):
    caption = fmt_row(row)
    photo_id = str(row.get("PhotoID", "")).strip()

    print("PHOTO_ID:", photo_id)

    if photo_id and photo_id.lower() != "nan":
        try:
            await update.message.reply_photo(
                photo=photo_id,
                caption=caption,
            )
            return
        except Exception as error:
            print("PHOTO ERROR:", error)

    await update.message.reply_text(caption)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Просто отправь номер детали или часть номера.\n"
        "Чтобы обновить базу — отправь Excel файл .xlsx.\n\n"
        "Удаление проданных позиций:\n"
        "/delete 58,73,102\n\n"
        "Отмена последнего удаления:\n"
        "/undo\n\n"
        "Если хочешь добавить фото:\n"
        "отправь мне фотографию, и я пришлю PhotoID."
    )


async def help_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "Команды:\n\n"
        "/start — старт\n"
        "/help — помощь\n"
        "/delete 58,73,102 — удалить строки Excel\n"
        "/undo — отменить последнее удаление\n\n"
        "Важно:\n"
        "в /delete указываются номера строк, "
        "которые видны слева в Excel.\n\n"
        "Можно написать:\n"
        "/delete 58,73,102\n\n"
        "Или:\n"
        "/delete 58 73 102\n\n"
        "Поиск:\n"
        "просто отправь номер детали или часть номера.\n\n"
        "Обновление базы:\n"
        "отправь Excel файл .xlsx.\n\n"
        "Фото:\n"
        "отправь боту фотографию, и я пришлю PhotoID.\n"
        "Потом вставь PhotoID в колонку PhotoID в Excel."
    )


async def delete_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not is_authorized(user.id):
        await update.message.reply_text(
            "⛔ У вас нет доступа к удалению позиций."
        )
        return

    full_text = update.message.text or ""

    # Убираем /delete или /delete@ИмяБота.
    raw_values = re.sub(
        r"^/delete(?:@\w+)?",
        "",
        full_text,
        flags=re.IGNORECASE,
    ).strip()

    if not raw_values:
        await update.message.reply_text(
            "Напиши номера строк после команды.\n\n"
            "Например:\n"
            "/delete 58,73,102"
        )
        return

    # Разрешаем только цифры, пробелы, запятые и точки с запятой.
    if not re.fullmatch(r"[\d,\s;]+", raw_values):
        await update.message.reply_text(
            "❌ Неверный формат.\n\n"
            "Используй только номера строк.\n"
            "Например:\n"
            "/delete 58,73,102"
        )
        return

    row_numbers = [
        int(number)
        for number in re.findall(r"\d+", raw_values)
    ]

    # Удаляем повторяющиеся номера.
    row_numbers = sorted(set(row_numbers))

    if not row_numbers:
        await update.message.reply_text(
            "❌ Не удалось найти номера строк."
        )
        return

    if len(row_numbers) > 500:
        await update.message.reply_text(
            "❌ За один раз можно удалить максимум 500 строк."
        )
        return

    try:
        async with excel_lock:
            items, invalid_rows = get_rows_preview(row_numbers)
    except Exception as error:
        await update.message.reply_text(
            f"⚠️ Ошибка при чтении Excel:\n{error}"
        )
        return

    if not items:
        await update.message.reply_text(
            "❌ Ни одной подходящей строки не найдено."
        )
        return

    # Сохраняем список строк отдельно для каждого пользователя.
    context.user_data["pending_delete_rows"] = [
        item["row"] for item in items
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Удалить",
                    callback_data="confirm_delete",
                ),
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data="cancel_delete",
                ),
            ]
        ]
    )

    preview_text = format_delete_preview(items, invalid_rows)

    await update.message.reply_text(
        preview_text,
        reply_markup=keyboard,
    )


async def delete_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    if not is_authorized(user.id):
        await query.edit_message_text(
            "⛔ У вас нет доступа к удалению позиций."
        )
        return

    action = query.data

    if action == "cancel_delete":
        context.user_data.pop("pending_delete_rows", None)

        await query.edit_message_text(
            "❌ Удаление отменено. Excel не изменён."
        )
        return

    if action != "confirm_delete":
        return

    row_numbers = context.user_data.get("pending_delete_rows")

    if not row_numbers:
        await query.edit_message_text(
            "⚠️ Список для удаления уже недействителен.\n"
            "Отправь команду /delete ещё раз."
        )
        return

    try:
        async with excel_lock:
            # Ещё раз проверяем строки прямо перед удалением.
            current_items, invalid_rows = get_rows_preview(
                row_numbers
            )

            if invalid_rows or len(current_items) != len(row_numbers):
                context.user_data.pop(
                    "pending_delete_rows",
                    None,
                )

                await query.edit_message_text(
                    "⚠️ Excel изменился после команды /delete.\n"
                    "Для безопасности удаление остановлено.\n\n"
                    "Отправь команду /delete ещё раз."
                )
                return

            backup_path = create_backup("before_delete")

            deleted_items = delete_excel_rows(row_numbers)

            # Запоминаем последнюю резервную копию для /undo.
            context.application.bot_data["last_backup"] = backup_path
            context.application.bot_data["last_delete_user"] = user.id
            context.application.bot_data[
                "last_deleted_items"
            ] = deleted_items

    except Exception as error:
        await query.edit_message_text(
            f"⚠️ Ошибка при удалении:\n{error}"
        )
        return

    context.user_data.pop("pending_delete_rows", None)

    lines = [
        f"✅ Полностью удалено: {len(deleted_items)}",
        "",
    ]

    shown_items = deleted_items[:40]

    for item in shown_items:
        line = f'{item["row"]} — {item["part_number"]}'

        if item["serial_number"]:
            line += f' | S/N: {item["serial_number"]}'

        lines.append(line)

    if len(deleted_items) > 40:
        lines.append("")
        lines.append(
            f"…и ещё {len(deleted_items) - 40} позиций."
        )

    lines.append("")
    lines.append(
        "Эти запчасти больше не находятся через поиск."
    )
    lines.append(
        "Для отмены последнего удаления отправь /undo."
    )

    await query.edit_message_text("\n".join(lines))


async def undo_cmd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not is_authorized(user.id):
        await update.message.reply_text(
            "⛔ У вас нет доступа к восстановлению Excel."
        )
        return

    backup_path = context.application.bot_data.get(
        "last_backup"
    )

    last_delete_user = context.application.bot_data.get(
        "last_delete_user"
    )

    if not backup_path or not os.path.exists(backup_path):
        await update.message.reply_text(
            "❌ Нет последнего удаления, которое можно отменить."
        )
        return

    if (
        ADMIN_ID
        and last_delete_user
        and str(last_delete_user) != str(user.id)
    ):
        await update.message.reply_text(
            "❌ Последнее удаление сделал другой пользователь."
        )
        return

    try:
        async with excel_lock:
            # Перед восстановлением тоже сохраняем текущую версию.
            create_backup("before_undo")
            shutil.copy2(backup_path, FILE_PATH)

            # Эту копию уже использовали.
            context.application.bot_data.pop(
                "last_backup",
                None,
            )
            context.application.bot_data.pop(
                "last_delete_user",
                None,
            )
            context.application.bot_data.pop(
                "last_deleted_items",
                None,
            )

    except Exception as error:
        await update.message.reply_text(
            f"⚠️ Не удалось восстановить Excel:\n{error}"
        )
        return

    await update.message.reply_text(
        "✅ Последнее удаление отменено.\n"
        "Предыдущая версия warehouse.xlsx восстановлена."
    )


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    if not user or not is_authorized(user.id):
        await update.message.reply_text(
            "⛔ У вас нет доступа к обновлению базы."
        )
        return

    doc = update.message.document

    if not doc:
        return

    name = doc.file_name or ""

    if not name.lower().endswith(".xlsx"):
        await update.message.reply_text(
            "❌ Пришли именно Excel файл (.xlsx)"
        )
        return

    temp_path = "warehouse_upload_temp.xlsx"

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(temp_path)

        # Проверяем файл до того, как заменять рабочую базу.
        test_df = pd.read_excel(temp_path)
        test_df.columns = [
            str(column).strip()
            for column in test_df.columns
        ]

        missing = [
            column
            for column in REQUIRED_COLUMNS
            if column not in test_df.columns
        ]

        if missing:
            os.remove(temp_path)

            await update.message.reply_text(
                "⚠️ В новом Excel не хватает колонок:\n"
                + ", ".join(missing)
            )
            return

        async with excel_lock:
            if os.path.exists(FILE_PATH):
                create_backup("before_upload")

            shutil.move(temp_path, FILE_PATH)

        # Финальная проверка уже рабочего файла.
        load_df()

    except Exception as error:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        await update.message.reply_text(
            f"⚠️ Не удалось загрузить таблицу:\n{error}"
        )
        return

    await update.message.reply_text(
        "✅ Таблица обновлена! Теперь можно искать."
    )


async def handle_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.message.photo:
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id

    await update.message.reply_text(
        f"PhotoID:\n{file_id}\n\n"
        "Скопируй это и вставь в колонку PhotoID в Excel."
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (update.message.text or "").strip()

    if not text:
        return

    query_norm = normalize_part_for_search(text)

    if not query_norm:
        await update.message.reply_text(
            "❓ Напиши номер детали."
        )
        return

    try:
        df = load_df()
    except Exception as error:
        await update.message.reply_text(
            f"⚠️ Ошибка: {error}"
        )
        return

    # Сначала ищем полное совпадение.
    exact_only = df[df["_pn_norm"] == query_norm]

    if not exact_only.empty:
        if len(exact_only) == 1:
            row = exact_only.iloc[0]
            await send_part_response(
                update,
                context,
                row,
            )
            return

        responses = [
            fmt_row(row)
            for _, row in exact_only.head(10).iterrows()
        ]

        message = "\n\n".join(responses)

        if len(exact_only) > 10:
            message += (
                "\n\nℹ️ Нашла несколько одинаковых позиций, "
                "показала первые 10."
            )

        await update.message.reply_text(message)
        return

    # Затем ищем по части номера.
    partial = df[
        df["_pn_norm"].str.contains(
            query_norm,
            na=False,
            regex=False,
        )
    ]

    if not partial.empty:
        if len(partial) == 1:
            row = partial.iloc[0]
            await send_part_response(
                update,
                context,
                row,
            )
            return

        responses = [
            fmt_row(row)
            for _, row in partial.head(10).iterrows()
        ]

        message = "\n\n".join(responses)

        if len(partial) > 10:
            message += (
                "\n\nℹ️ Нашла несколько вариантов, "
                "показала первые 10."
            )

        await update.message.reply_text(message)
        return

    # Если точного или частичного совпадения нет,
    # ищем похожие номера.
    pn_list = (
        df["_pn_norm"]
        .dropna()
        .astype(str)
        .tolist()
    )

    close_matches = difflib.get_close_matches(
        query_norm,
        pn_list,
        n=10,
        cutoff=0.75,
    )

    if close_matches:
        fuzzy = df[
            df["_pn_norm"].isin(close_matches)
        ]

        responses = [
            fmt_row(row)
            for _, row in fuzzy.head(10).iterrows()
        ]

        message = (
            "🤔 Точного совпадения нет, "
            "но нашла похожие:\n\n"
            + "\n\n".join(responses)
        )

        if len(fuzzy) > 10:
            message += "\n\nℹ️ Показала первые 10."

        await update.message.reply_text(message)
        return

    await update.message.reply_text(
        "❓ Ничего не нашла по этому запросу"
    )


def main():
    if not TOKEN:
        raise RuntimeError(
            "TOKEN не задан. Добавь TOKEN в Railway Variables."
        )

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_cmd)
    )

    app.add_handler(
        CommandHandler("delete", delete_cmd)
    )

    app.add_handler(
        CommandHandler("undo", undo_cmd)
    )

    app.add_handler(
        CallbackQueryHandler(
            delete_callback,
            pattern=r"^(confirm_delete|cancel_delete)$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_document,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    print("🤖 Warehouse bot started")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
