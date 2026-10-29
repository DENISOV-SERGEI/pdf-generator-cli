import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ALLOWED_USER_IDS", "123")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_generator


class BotGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name) / "output"
        self.database_path = self.output_dir / "issued_documents.sqlite3"
        self.output_patch = patch.object(bot_generator, "OUTPUT_DIR", self.output_dir)
        self.database_patch = patch.object(
            bot_generator, "DATABASE_PATH", self.database_path
        )
        self.users_patch = patch.object(bot_generator, "ALLOWED_USER_IDS", {123})
        self.output_patch.start()
        self.database_patch.start()
        self.users_patch.start()
        bot_generator.init_project()

    def tearDown(self):
        self.users_patch.stop()
        self.database_patch.stop()
        self.output_patch.stop()
        self.temp_dir.cleanup()

    def test_parse_batch_accepts_multiple_documents(self):
        documents = bot_generator.parse_document_batch(
            "Alice : Python : course-1\nBob : Design : course-2"
        )

        self.assertEqual(
            documents,
            [("Alice", "Python", "course-1"), ("Bob", "Design", "course-2")],
        )

    def test_rate_limit_blocks_requests_over_ten_documents(self):
        now = datetime.now(timezone.utc)
        issued_at = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO issued_documents
                    (document_id, pdf_filename, issued_at, issued_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (f"old-{index}", f"old-{index}.pdf", issued_at, 123)
                        for index in range(8)
                    ],
                )
            with self.assertRaises(bot_generator.RateLimitExceeded):
                bot_generator.ensure_rate_limit(connection, 123, 3, now)
            bot_generator.ensure_rate_limit(connection, 123, 2, now)

    def test_create_batch_rejects_over_limit_before_generating(self):
        issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO issued_documents
                    (document_id, pdf_filename, issued_at, issued_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (f"old-{index}", f"old-{index}.pdf", issued_at, 123)
                        for index in range(10)
                    ],
                )

        message = SimpleNamespace(
            chat=SimpleNamespace(id=123),
            message_id=1,
            from_user=SimpleNamespace(id=123),
        )
        with self.assertRaises(bot_generator.RateLimitExceeded):
            bot_generator.create_pdf_batch(
                [("Alice", "Python", "new-id")],
                "diploma_template.html",
                message,
                "2026-10-29",
            )
        self.assertEqual(list(self.output_dir.glob("document_*.pdf")), [])

    def test_expired_pdf_is_deleted_but_ledger_entry_remains(self):
        pdf_path = self.output_dir / "expired.pdf"
        pdf_path.write_bytes(b"%PDF-test")
        expired_at = (
            datetime.now(timezone.utc) - timedelta(days=31)
        ).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO issued_documents
                    (document_id, pdf_filename, issued_at, issued_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("expired-id", pdf_path.name, expired_at, 123),
                )

        bot_generator.cleanup_expired_documents()

        self.assertFalse(pdf_path.exists())
        with closing(sqlite3.connect(self.database_path)) as connection:
            result = connection.execute(
                "SELECT document_id FROM issued_documents WHERE document_id = ?",
                ("expired-id",),
            ).fetchone()
        self.assertEqual(result, ("expired-id",))

    def test_resend_sends_saved_pdf_to_authorized_private_user(self):
        pdf_path = self.output_dir / "document-1.pdf"
        pdf_path.write_bytes(b"%PDF-test")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO issued_documents
                    (document_id, pdf_filename, issued_at, issued_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "document-1",
                        pdf_path.name,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        123,
                    ),
                )

        sent = []
        message = SimpleNamespace(
            text="/resend document-1",
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=123),
        )
        with patch.object(
            bot_generator.bot,
            "send_document",
            side_effect=lambda chat_id, file, caption: sent.append(file.read()),
        ):
            bot_generator.resend_document(message)

        self.assertEqual(sent, [b"%PDF-test"])

    def test_group_chat_cannot_start_document_generation(self):
        replies = []
        message = SimpleNamespace(
            text="/diploma",
            chat=SimpleNamespace(id=-100, type="group"),
            from_user=SimpleNamespace(id=123),
        )
        with patch.object(
            bot_generator.bot,
            "reply_to",
            side_effect=lambda _message, text, **_kwargs: replies.append(text),
        ):
            bot_generator.choose_document(message)

        self.assertIn("только в личном чате", replies[0])


if __name__ == "__main__":
    unittest.main()
