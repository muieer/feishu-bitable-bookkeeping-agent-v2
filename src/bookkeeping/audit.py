import contextvars
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

message_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "message_id", default=None
)


class Redactor:
    def __init__(self, secrets: list[str]):
        self.secrets = sorted([value for value in secrets if value], key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{10,}", "[REDACTED]", value)
        value = re.sub(r"wss?://[^\s\"']+", "[REDACTED_WS_URL]", value)
        return re.sub(
            r"(?i)((?:app[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"authorization|password)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}]+",
            r"\1[REDACTED]",
            value,
        )

    def clean(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {
                key: "[REDACTED]"
                if re.search(
                    r"secret|api.?key|authorization|password|access.?token|refresh.?token",
                    str(key),
                    re.I,
                )
                else self.clean(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return self.text(str(value))


class PrivateRotatingHandler(RotatingFileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        if self._closed:
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        # Standard logging prints the original, unredacted record on I/O errors.
        try:
            os.write(2, b"bookkeeping audit log write failed\n")
        except OSError:
            pass

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


class SafeFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__("%(message)s")
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        safe = self.redactor.text(super().format(record))
        if record.name.startswith("bookkeeping.audit."):
            return safe
        return json.dumps(
            {
                "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "event": "sdk_log",
                "logger": record.name,
                "level": record.levelname,
                "message": safe,
            },
            ensure_ascii=False,
        )


class Audit:
    def __init__(self, path: Path, secrets: list[str]):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        self.redactor = Redactor(secrets)
        self.logger = logging.getLogger(f"bookkeeping.audit.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = PrivateRotatingHandler(
            path, maxBytes=10_000_000, backupCount=3, encoding="utf-8"
        )
        self.handler.setFormatter(SafeFormatter(self.redactor))
        self.logger.addHandler(self.handler)

    def event(self, event: str, **data: Any) -> None:
        payload = {
            "time": datetime.now(UTC).isoformat(),
            "event": event,
            "message_id": message_context.get(),
            **data,
        }
        self.logger.info(json.dumps(self.redactor.clean(payload), ensure_ascii=False))

    def capture_sdk_logs(self) -> None:
        # Replace SDK handlers before connecting; never let connection credentials reach stdout.
        for name in ("Lark", "httpx", "httpx2", "openai", "asyncio"):
            logger = logging.getLogger(name)
            logger.handlers = [self.handler]
            logger.propagate = False
            logger.setLevel(logging.WARNING)

    def close(self) -> None:
        self.handler.close()
        self.logger.removeHandler(self.handler)


class RequestBudget:
    """Network-level counter, independent of agent/tool retry accounting."""

    def __init__(self, limit: int | None = None, path: Path | None = None):
        self.limit = limit
        self.path = path
        self._lock = threading.Lock()
        self.count = 0
        if path and path.exists():
            self.count = sum(1 for line in path.read_text().splitlines() if line.strip())

    def take(self) -> int:
        with self._lock:
            if self.limit is not None and self.count >= self.limit:
                raise RuntimeError("实际模型请求预算已用完，未发出额外请求")
            self.count += 1
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps({"request": self.count, "time": datetime.now(UTC).isoformat()})
                        + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            return self.count


class ModelAuditHooks:
    def __init__(self, audit: Audit, budget: RequestBudget):
        self.audit, self.budget = audit, budget

    async def request(self, request: httpx.Request) -> None:
        if request.url.path.endswith("/chat/completions"):
            request.extensions["audit_request"] = self.budget.take()
            request.extensions["audit_start"] = time.monotonic()
            self.audit.event(
                "model_request",
                request_number=request.extensions["audit_request"],
                body=json.loads(request.content),
            )

    async def response(self, response: httpx.Response) -> None:
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            body = response.text[:20000]
        started = response.request.extensions.get("audit_start", time.monotonic())
        self.audit.event(
            "model_response",
            request_number=response.request.extensions.get("audit_request"),
            status=response.status_code,
            duration=round(time.monotonic() - started, 3),
            body=body,
        )
