"""CLI-генератор PDF-счетов на основе CSV/JSON и HTML-шаблонов."""

import csv
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ПАПКА_ПРОЕКТА = Path(__file__).resolve().parent
ПАПКА_ДАННЫХ = ПАПКА_ПРОЕКТА / "data"
ПАПКА_ШАБЛОНОВ = ПАПКА_ПРОЕКТА / "templates"
ПАПКА_РЕЗУЛЬТАТОВ = ПАПКА_ПРОЕКТА / "output"
ШАБЛОН_ПЛЕЙСХОЛДЕРА = re.compile(r"\{\{\s*([a-zA-Z_][\w.-]*)\s*\}\}")
ЗАПРЕЩЁННЫЕ_СИМВОЛЫ_ИМЕНИ = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def выбрать_файл(заголовок: str, файлы: list[Path]) -> Path | None:
    """Показать нумерованное меню и вернуть выбранный файл."""
    if not файлы:
        print(f"Ошибка: {заголовок.lower()} не найдены.")
        return None

    print(f"\n{заголовок}:")
    for номер, файл in enumerate(файлы, start=1):
        print(f"  {номер}. {файл.name}")

    while True:
        выбор = input("Введите номер файла: ").strip()
        try:
            номер = int(выбор)
        except ValueError:
            номер = 0

        if 1 <= номер <= len(файлы):
            return файлы[номер - 1]
        print(f"Неверный пункт меню. Введите число от 1 до {len(файлы)}.")


def загрузить_заказы(путь: Path) -> list[dict[str, Any]]:
    """Прочитать список заказов из CSV или JSON."""
    if путь.suffix.lower() == ".csv":
        with путь.open("r", encoding="utf-8-sig", newline="") as файл:
            читатель = csv.DictReader(файл)
            if not читатель.fieldnames or "invoice_id" not in читатель.fieldnames:
                raise ValueError('В CSV-файле отсутствует столбец "invoice_id".')
            заказы = list(читатель)
    else:
        with путь.open("r", encoding="utf-8-sig") as файл:
            данные = json.load(файл)

        if isinstance(данные, list):
            заказы = данные
        elif isinstance(данные, dict):
            for ключ in ("orders", "invoices", "data"):
                if ключ in данные:
                    заказы = данные[ключ]
                    break
            else:
                заказы = [данные]
        else:
            raise ValueError("JSON должен содержать объект или список заказов.")

        if not isinstance(заказы, list) or not all(
            isinstance(заказ, dict) for заказ in заказы
        ):
            raise ValueError(
                'Заказы в JSON должны быть списком объектов (например, под ключом "orders").'
            )

    if not заказы:
        raise ValueError("В выбранном файле нет заказов.")

    for заказ in заказы:
        if "invoice_id" not in заказ or заказ["invoice_id"] is None:
            raise ValueError('У одного или нескольких заказов отсутствует поле "invoice_id".')

    идентификаторы = [str(заказ["invoice_id"]) for заказ in заказы]
    if len(идентификаторы) != len(set(идентификаторы)):
        raise ValueError('В файле есть повторяющиеся значения "invoice_id".')
    return заказы


def выбрать_заказ(заказы: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Показать доступные invoice_id и выбрать заказ по номеру или ID."""
    print("\nДоступные заказы:")
    for номер, заказ in enumerate(заказы, start=1):
        print(f"  {номер}. {заказ['invoice_id']}")

    идентификаторы = {str(заказ["invoice_id"]): заказ for заказ in заказы}
    while True:
        выбор = input("Введите номер заказа или invoice_id: ").strip()
        try:
            номер = int(выбор)
        except ValueError:
            номер = 0

        if 1 <= номер <= len(заказы):
            return заказы[номер - 1]
        if выбор in идентификаторы:
            return идентификаторы[выбор]
        print("Такого заказа нет. Выберите номер из списка или введите invoice_id.")


def подставить_значения(шаблон: str, заказ: dict[str, Any]) -> str:
    """Заменить плейсхолдеры значениями заказа с экранированием HTML."""

    def заменить(совпадение: re.Match[str]) -> str:
        имя_поля = совпадение.group(1)
        if имя_поля not in заказ:
            raise ValueError(f'В данных заказа отсутствует поле "{имя_поля}".')
        значение = заказ[имя_поля]
        return html.escape("" if значение is None else str(значение), quote=True)

    return ШАБЛОН_ПЛЕЙСХОЛДЕРА.sub(заменить, шаблон)


def безопасный_идентификатор(значение: Any) -> str:
    """Подготовить ID для имени файла на Windows, macOS и Linux."""
    идентификатор = ЗАПРЕЩЁННЫЕ_СИМВОЛЫ_ИМЕНИ.sub("_", str(значение)).strip(" .")
    if not идентификатор:
        raise ValueError("invoice_id нельзя использовать в имени PDF-файла.")
    return идентификатор


def открыть_файл(путь: Path) -> None:
    """Открыть файл PDF в приложении, назначенном в операционной системе."""
    if os.name == "nt":
        os.startfile(str(путь))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(путь)])
    else:
        subprocess.Popen(["xdg-open", str(путь)])


def main() -> int:
    файлы_данных = sorted(
        (
            файл
            for файл in ПАПКА_ДАННЫХ.glob("*")
            if файл.is_file() and файл.suffix.lower() in {".csv", ".json"}
        ),
        key=lambda файл: файл.name.lower(),
    )
    файл_данных = выбрать_файл("Файлы данных", файлы_данных)
    if файл_данных is None:
        return 1

    шаблоны = sorted(
        (
            файл
            for файл in ПАПКА_ШАБЛОНОВ.glob("*")
            if файл.is_file() and файл.suffix.lower() == ".html"
        ),
        key=lambda файл: файл.name.lower(),
    )
    файл_шаблона = выбрать_файл("HTML-шаблоны", шаблоны)
    if файл_шаблона is None:
        return 1

    try:
        заказы = загрузить_заказы(файл_данных)
        заказ = выбрать_заказ(заказы)
        if заказ is None:
            return 1

        with файл_шаблона.open("r", encoding="utf-8-sig") as файл:
            содержимое_шаблона = файл.read()
        готовый_html = подставить_значения(содержимое_шаблона, заказ)

        from weasyprint import HTML

        ПАПКА_РЕЗУЛЬТАТОВ.mkdir(parents=True, exist_ok=True)
        имя_pdf = f"invoice_{безопасный_идентификатор(заказ['invoice_id'])}.pdf"
        путь_pdf = ПАПКА_РЕЗУЛЬТАТОВ / имя_pdf
        HTML(
            string=готовый_html,
            base_url=str(файл_шаблона.parent),
        ).write_pdf(str(путь_pdf))

        print(f"\nPDF успешно создан: {путь_pdf}")
        открыть_файл(путь_pdf)
        return 0
    except ImportError as ошибка:
        print(
            "Ошибка: библиотека WeasyPrint не установлена. "
            "Установите её командой: pip install weasyprint"
        )
        print(f"Подробности: {ошибка}")
    except (OSError, ValueError, csv.Error, json.JSONDecodeError) as ошибка:
        print(f"Ошибка: {ошибка}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())