import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .domain import EntryResult, Expense, TaskReport, WriteOutcome


@dataclass(frozen=True)
class Message:
    message_id: str
    chat_id: str
    text: str
    execution_date: date
    state: str
    card_id: str | None


class Journal:
    """Durable submission reservations; a submitted row is never automatically resubmitted."""

    def __init__(self, path: Path, namespace: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL,
                execution_date TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                card_id TEXT, report TEXT, notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS writes (
                message_id TEXT NOT NULL, source_start INTEGER NOT NULL,
                source_end INTEGER NOT NULL,
                expense TEXT NOT NULL, state TEXT NOT NULL, record_id TEXT, reason TEXT NOT NULL,
                PRIMARY KEY (message_id, source_start, source_end),
                FOREIGN KEY (message_id) REFERENCES messages(message_id)
            );
        """)
        previous = self.connection.execute(
            "SELECT value FROM metadata WHERE key='namespace'"
        ).fetchone()
        if previous and previous[0] != namespace:
            self.connection.close()
            raise ValueError("执行日志属于其他应用或账本，请为新目标配置独立的 RUNTIME_DIR")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('namespace', ?)", (namespace,)
            )

    def accept(self, message_id: str, chat_id: str, text: str, today: date) -> bool:
        if not message_id or not chat_id:
            raise ValueError("消息必须有平台消息 ID 和会话 ID")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO messages(message_id, chat_id, text, execution_date) "
                "VALUES (?, ?, ?, ?)",
                (message_id, chat_id, text, today.isoformat()),
            )
            return cursor.rowcount == 1

    def message(self, message_id: str) -> Message:
        row = self.connection.execute(
            "SELECT * FROM messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return Message(
            row["message_id"],
            row["chat_id"],
            row["text"],
            date.fromisoformat(row["execution_date"]),
            row["state"],
            row["card_id"],
        )

    def start(self, message_id: str, today: date) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE messages SET state='running', execution_date=? "
                "WHERE message_id=? AND state='queued'",
                (today.isoformat(), message_id),
            )
            return cursor.rowcount == 1

    def reserve(
        self, message_id: str, span: tuple[int, int], expense: Expense
    ) -> EntryResult | None:
        with self.connection:
            # Overlap detects retry aliases such as a full sentence vs. its amount-bearing clause.
            old = self.connection.execute(
                "SELECT * FROM writes WHERE message_id=? AND source_start < ? AND source_end > ?",
                (message_id, span[1], span[0]),
            ).fetchone()
            if old:
                return self._entry(old)
            self.connection.execute(
                "INSERT INTO writes VALUES (?, ?, ?, ?, 'submitted', NULL, ?)",
                (message_id, *span, expense.model_dump_json(), "写入已开始，结果尚未确认"),
            )
        return None

    def complete_write(self, message_id: str, span: tuple[int, int], outcome: WriteOutcome) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE writes SET state=?, record_id=?, reason=? "
                "WHERE message_id=? AND source_start=? AND source_end=?",
                (outcome.status, outcome.record_id, outcome.reason, message_id, *span),
            )

    def overlaps(self, message_id: str, span: tuple[int, int]) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM writes WHERE message_id=? AND source_start < ? AND source_end > ?",
                (message_id, span[1], span[0]),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _entry(row: sqlite3.Row) -> EntryResult:
        status = "uncertain" if row["state"] == "submitted" else row["state"]
        return EntryResult(
            expense=Expense.model_validate_json(row["expense"]),
            outcome=WriteOutcome(status=status, record_id=row["record_id"], reason=row["reason"]),
        )

    def entries(self, message_id: str) -> list[EntryResult]:
        rows = self.connection.execute(
            "SELECT * FROM writes WHERE message_id=? ORDER BY source_start", (message_id,)
        ).fetchall()
        return [self._entry(row) for row in rows]

    def finish(self, message_id: str, report: TaskReport) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET state='done', report=? WHERE message_id=?",
                (report.model_dump_json(), message_id),
            )

    def report(self, message_id: str) -> TaskReport | None:
        row = self.connection.execute(
            "SELECT report FROM messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return TaskReport.model_validate_json(row[0]) if row and row[0] else None

    def set_card(self, message_id: str, card_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET card_id=? WHERE message_id=?", (card_id, message_id)
            )

    def notified(self, message_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE messages SET notified=1 WHERE message_id=?", (message_id,)
            )

    def pending_notifications(self) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT message_id FROM messages WHERE state='done' AND notified=0"
            )
        ]

    def queued(self) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT message_id FROM messages WHERE state='queued' ORDER BY rowid"
            )
        ]

    def recover_interrupted(self) -> list[str]:
        interrupted = [
            row[0]
            for row in self.connection.execute(
                "SELECT message_id FROM messages WHERE state='running'"
            )
        ]
        with self.connection:
            self.connection.execute(
                "UPDATE writes SET state='uncertain', reason=? WHERE state='submitted'",
                ("处理期间进程中断，写入结果无法确认，请检查账本；不会自动重试",),
            )
            for message_id in interrupted:
                self.finish(
                    message_id,
                    TaskReport(
                        entries=self.entries(message_id),
                        notice="上次处理被中断，消息可能尚未处理完整。已提交但未确认的消费请核对账本。",
                    ),
                )
        return interrupted

    def close(self) -> None:
        self.connection.close()
