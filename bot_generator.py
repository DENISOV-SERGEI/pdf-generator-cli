import logging
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from html import escape
from pathlib import Path
from datetime import datetime, timedelta, timezone

import telebot
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DATABASE_PATH = OUTPUT_DIR / "issued_documents.sqlite3"
MAX_BATCH_SIZE = 10
MAX_PDFS_PER_WINDOW = 10
RATE_LIMIT_WINDOW_MINUTES = 10
DEFAULT_PDF_RETENTION_DAYS = 30
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in the .env file or environment.")

try:
    ALLOWED_USER_IDS = {
        int(user_id.strip())
        for user_id in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if user_id.strip()
    }
    PDF_RETENTION_DAYS = int(
        os.getenv("PDF_RETENTION_DAYS", str(DEFAULT_PDF_RETENTION_DAYS))
    )
except ValueError as error:
    raise RuntimeError(
        "ALLOWED_USER_IDS must be comma-separated Telegram user IDs and "
        "PDF_RETENTION_DAYS must be a positive integer."
    ) from error

if PDF_RETENTION_DAYS < 1:
    raise RuntimeError("PDF_RETENTION_DAYS must be a positive integer.")
if any(user_id <= 0 for user_id in ALLOWED_USER_IDS):
    raise RuntimeError("ALLOWED_USER_IDS must contain positive Telegram user IDs.")

bot = telebot.TeleBot(TOKEN)


class RateLimitExceeded(Exception):
    pass


def init_project():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS issued_documents (
                    document_id TEXT PRIMARY KEY,
                    pdf_filename TEXT NOT NULL UNIQUE,
                    issued_at TEXT NOT NULL,
                    issued_by INTEGER NOT NULL
                )
                """
            )


def cleanup_expired_documents():
    init_project()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PDF_RETENTION_DAYS)).isoformat(
        timespec="seconds"
    )
    output_dir = OUTPUT_DIR.resolve()

    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        with connection:
            expired = connection.execute(
                "SELECT document_id, pdf_filename FROM issued_documents WHERE issued_at < ?",
                (cutoff,),
            ).fetchall()
            for document_id, filename in expired:
                pdf_path = (OUTPUT_DIR / filename).resolve()
                if pdf_path.parent != output_dir or pdf_path.suffix.lower() != ".pdf":
                    logging.error(
                        "Skipping unsafe PDF path in database for ID %s", document_id
                    )
                    continue
                pdf_path.unlink(missing_ok=True)


def is_authorized(message):
    user = message.from_user
    return user is not None and user.id in ALLOWED_USER_IDS


def is_private_chat(message):
    return message.chat.type == "private"


def ensure_rate_limit(connection, user_id, requested_count, now=None):
    now = now or datetime.now(timezone.utc)
    window_start = (
        now - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
    ).isoformat(timespec="seconds")
    issued_count = connection.execute(
        "SELECT COUNT(*) FROM issued_documents WHERE issued_by = ? AND issued_at >= ?",
        (user_id, window_start),
    ).fetchone()[0]
    if issued_count + requested_count > MAX_PDFS_PER_WINDOW:
        raise RateLimitExceeded


def find_issued_pdf(document_id):
    with closing(sqlite3.connect(DATABASE_PATH)) as connection:
        row = connection.execute(
            "SELECT pdf_filename FROM issued_documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()
    if row is None:
        return None

    output_dir = OUTPUT_DIR.resolve()
    pdf_path = (OUTPUT_DIR / row[0]).resolve()
    if pdf_path.parent != output_dir or pdf_path.suffix.lower() != ".pdf":
        logging.error("Unsafe PDF path stored for document ID %s", document_id)
        return None
    return pdf_path


def block_resource_fetcher(url):
    raise ValueError("External resources are disabled for PDF rendering.")


def register_retry(message, template_name, prompt):
    reply = bot.reply_to(message, prompt, parse_mode="Markdown")
    bot.register_next_step_handler(reply, process_document_generation, template_name)


def parse_document_batch(text):
    entries = [line.strip() for line in text.splitlines() if line.strip()]
    if not entries:
        raise ValueError("No document data provided.")
    if len(entries) > MAX_BATCH_SIZE:
        raise ValueError(f"At most {MAX_BATCH_SIZE} documents can be generated at once.")

    documents = []
    seen_ids = set()
    for line_number, entry in enumerate(entries, start=1):
        parts = [part.strip() for part in entry.split(':', 2)]
        if len(parts) != 3:
            raise ValueError(f"Line {line_number} must contain three fields.")

        customer_name, product, invoice_id = parts
        if (
            not customer_name
            or not product
            or not invoice_id
            or len(customer_name) > 120
            or len(product) > 200
            or not re.fullmatch(r"[\w-]{1,64}", invoice_id)
        ):
            raise ValueError(f"Line {line_number} contains invalid fields.")
        if invoice_id in seen_ids:
            raise ValueError(f"Line {line_number} repeats an ID.")

        seen_ids.add(invoice_id)
        documents.append((customer_name, product, invoice_id))

    return documents


def create_pdf_batch(documents, template_name, message, date_now):
    init_project()
    template_path = BASE_DIR / "templates" / template_name
    html_content = template_path.read_text(encoding="utf-8")
    generated_files = []
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    published_paths = []

    try:
        connection.execute("BEGIN IMMEDIATE")
        issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ensure_rate_limit(connection, message.from_user.id, len(documents))
        for customer_name, product, invoice_id in documents:
            output_filename = (
                f"document_{invoice_id}_{message.chat.id}_{message.message_id}.pdf"
            )
            connection.execute(
                """
                INSERT INTO issued_documents (document_id, pdf_filename, issued_at, issued_by)
                VALUES (?, ?, ?, ?)
                """,
                (invoice_id, output_filename, issued_at, message.from_user.id),
            )

        from weasyprint import HTML

        with tempfile.TemporaryDirectory(dir=OUTPUT_DIR) as temp_dir:
            for index, (customer_name, product, invoice_id) in enumerate(documents, start=1):
                document_html = html_content.replace(
                    '{{ customer_name }}', escape(customer_name)
                )
                document_html = document_html.replace('{{ product }}', escape(product))
                document_html = document_html.replace('{{ invoice_id }}', escape(invoice_id))
                document_html = document_html.replace('{{ date }}', date_now)

                temp_html_path = Path(temp_dir) / f"document_{index}.html"
                temp_pdf_path = Path(temp_dir) / f"document_{index}.pdf"
                output_pdf_path = OUTPUT_DIR / (
                    f"document_{invoice_id}_{message.chat.id}_{message.message_id}.pdf"
                )
                temp_html_path.write_text(document_html, encoding="utf-8")
                HTML(
                    filename=str(temp_html_path),
                    url_fetcher=block_resource_fetcher,
                ).write_pdf(str(temp_pdf_path))
                generated_files.append((temp_pdf_path, output_pdf_path))

            for temp_pdf_path, output_pdf_path in generated_files:
                temp_pdf_path.replace(output_pdf_path)
                published_paths.append(output_pdf_path)

        connection.commit()
        return [output_pdf_path for _, output_pdf_path in generated_files]
    except (OSError, ValueError, RuntimeError, sqlite3.Error, RateLimitExceeded):
        connection.rollback()
        for output_pdf_path in published_paths:
            output_pdf_path.unlink(missing_ok=True)
        raise
    finally:
        connection.close()


@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        "🏆 **Привет! Я ИИ-генератор премиальных документов.**\n\n"
        "Я могу мгновенно создать роскошный Диплом или Подарочный сертификат в формате PDF.\n\n"
        "Выбери, какой документ ты хочешь создать, с помощью команд:\n"
        "📜 /diploma — Выпуск Именного Диплома\n"
        "🎁 /gift — Создание Подарочного Сертификата\n"
        "📨 /resend ID — Повторно отправить выданный PDF"
    )
    bot.reply_to(message, welcome_text, parse_mode='Markdown')


@bot.message_handler(commands=['myid'])
def send_user_id(message):
    if message.from_user is None:
        bot.reply_to(message, "Не удалось определить ваш Telegram ID.")
        return
    bot.reply_to(
        message,
        f"Ваш Telegram ID: `{message.from_user.id}`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=['resend'])
def resend_document(message):
    if not is_private_chat(message):
        bot.reply_to(message, "⛔ Повторная отправка доступна только в личном чате с ботом.")
        return
    if not is_authorized(message):
        bot.reply_to(message, "⛔ У вас нет доступа к документам.")
        return

    command_parts = (message.text or "").split(maxsplit=1)
    if len(command_parts) != 2 or not re.fullmatch(r"[\w-]{1,64}", command_parts[1].strip()):
        bot.reply_to(message, "Использование: `/resend ID-документа`", parse_mode="Markdown")
        return

    document_id = command_parts[1].strip()
    try:
        cleanup_expired_documents()
        pdf_path = find_issued_pdf(document_id)
    except (OSError, sqlite3.Error):
        logging.exception("Could not look up document ID %s", document_id)
        bot.reply_to(message, "💥 Не удалось найти документ в журнале.")
        return

    if pdf_path is None:
        bot.reply_to(
            message,
            "PDF с таким ID не найден или срок хранения файла истёк.",
        )
        return

    try:
        with pdf_path.open("rb") as pdf_file:
            bot.send_document(
                message.chat.id,
                pdf_file,
                caption=f"Повторная отправка документа {document_id}",
            )
    except (OSError, telebot.apihelper.ApiTelegramException):
        logging.exception("Could not resend PDF for document ID %s", document_id)
        bot.reply_to(message, "💥 Не удалось отправить PDF. Попробуй позже.")


@bot.message_handler(commands=['diploma', 'gift'])
def choose_document(message):
    if not is_private_chat(message):
        bot.reply_to(message, "⛔ Выпуск документов доступен только в личном чате с ботом.")
        return

    if not is_authorized(message):
        if not ALLOWED_USER_IDS:
            bot.reply_to(
                message,
                "Бот ещё не настроен для выпуска документов. "
                "Добавьте свой Telegram ID в ALLOWED_USER_IDS в файле .env.",
            )
        else:
            bot.reply_to(message, "⛔ У вас нет доступа к выпуску документов.")
        return

    try:
        init_project()
        cleanup_expired_documents()
    except (OSError, sqlite3.Error):
        logging.exception("Could not initialize document storage")
        bot.reply_to(message, "💥 Не удалось подготовить хранилище документов.")
        return
    cmd = message.text.split('@', 1)[0].lstrip('/')

    if cmd == 'diploma':
        msg = bot.reply_to(
            message,
            "✍️ **Выпуск Диплома.** Пришли мне данные строго по шаблону:\n"
            "`ФИО : Название курса : ID-Документа`",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, process_document_generation, 'diploma_template.html')
    elif cmd == 'gift':
        msg = bot.reply_to(
            message,
            "✍️ **Создание Сертификата.** Пришли мне данные строго по шаблону:\n"
            "`Имя Фамилия : Название услуги/СПА : ID-Документа`",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, process_document_generation, 'gift_certificate.html')

def process_document_generation(message, template_name):
    if not is_private_chat(message):
        bot.reply_to(message, "⛔ Выпуск документов доступен только в личном чате с ботом.")
        return

    if not is_authorized(message):
        bot.reply_to(message, "⛔ У вас нет доступа к выпуску документов.")
        return

    if message.text and message.text.startswith('/'):
        bot.reply_to(message, "Действие отменено.")
        return

    if not message.text:
        register_retry(
            message,
            template_name,
            "❌ Нужен текст. Каждую запись пришли с новой строки:\n"
            "`Имя : Услуга/Курс : ID`",
        )
        return

    try:
        documents = parse_document_batch(message.text)
    except ValueError as error:
        register_retry(
            message,
            template_name,
            f"❌ {error}\n"
            f"Пришли до {MAX_BATCH_SIZE} записей, каждую с новой строки:\n"
            "`Имя (до 120 символов) : Услуга/Курс (до 200 символов) : "
            "уникальный ID (до 64 букв, цифр или дефисов)`",
        )
        return

    try:
        cleanup_expired_documents()
    except (OSError, sqlite3.Error):
        logging.exception("Could not clean up expired documents")
        bot.reply_to(message, "💥 Не удалось подготовить хранилище документов.")
        return

    date_now = datetime.now().strftime("%Y-%m-%d")
    try:
        bot.reply_to(
            message,
            f"⏳ Генерирую PDF-документы: {len(documents)} шт. Пожалуйста, подожди.",
        )
        generated_files = create_pdf_batch(documents, template_name, message, date_now)
    except sqlite3.IntegrityError:
        bot.reply_to(
            message,
            "❌ Один или несколько ID уже использовались. "
            "Используйте новые уникальные ID.",
        )
        return
    except RateLimitExceeded:
        bot.reply_to(
            message,
            f"⏳ Лимит: не более {MAX_PDFS_PER_WINDOW} документов за "
            f"{RATE_LIMIT_WINDOW_MINUTES} минут. Попробуй позже.",
        )
        return
    except (
        OSError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
        telebot.apihelper.ApiTelegramException,
    ):
        logging.exception("PDF generation failed for template %s", template_name)
        bot.reply_to(message, "💥 Не удалось создать PDF. Попробуй позже.")
        return

    for index, output_pdf_path in enumerate(generated_files, start=1):
        try:
            with output_pdf_path.open('rb') as pdf_file:
                bot.send_document(
                    message.chat.id,
                    pdf_file,
                    caption=f"✨ Документ {index} из {len(generated_files)}",
                )
        except (OSError, telebot.apihelper.ApiTelegramException):
            logging.exception("PDF was saved but could not be sent: %s", output_pdf_path)
            bot.reply_to(
                message,
                f"PDF сохранён в output, но отправить документ {index} не удалось. "
                "Можно отправить его повторно из папки output.",
            )
            break

if __name__ == "__main__":
    init_project()
    cleanup_expired_documents()
    if not ALLOWED_USER_IDS:
        logging.warning("No allowed users configured; document generation is disabled.")
    print("🤖 Бот успешно запущен и слушает команды...")
    bot.infinity_polling()
