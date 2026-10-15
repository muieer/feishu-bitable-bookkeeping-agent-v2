import argparse
import asyncio
import fcntl
import hashlib
import os
import signal
from contextlib import contextmanager
from pathlib import Path

import httpx
from pydantic import ValidationError

from .agent import create_agent
from .audit import Audit, ModelAuditHooks, RequestBudget
from .cards import CardReporter
from .channel import ChannelApplication, create_channel
from .cli import LarkCliLedger
from .config import Settings, load_settings
from .journal import Journal
from .knowledge import Knowledge
from .model import create_model
from .service import BookkeepingService


@contextmanager
def process_lock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("该执行日志已有进程使用，不能同时启动第二个记账进程") from error
        yield
    finally:
        os.close(fd)


def journal_namespace(settings: Settings) -> str:
    return hashlib.sha256(
        (
            settings.lark_app_id
            + settings.bitable_table_id
            + settings.bitable_base_token.get_secret_value()
        ).encode()
    ).hexdigest()


async def run(args, settings: Settings, audit: Audit) -> None:
    knowledge = Knowledge(settings.project_dir, settings.bitable_table_id)
    ledger = LarkCliLedger(settings, audit)
    if args.command == "doctor":
        await ledger.check_schema()
        print("分类知识与账本字段检查通过；CLI 使用已配置身份。")
        if args.websocket:
            channel = create_channel(settings, audit)
            try:
                await channel.connect_until_ready(timeout=30)
                print("飞书 WebSocket 长连接握手成功；本次检查未处理消息或写入消费。")
            finally:
                await channel.disconnect()
        return
    with process_lock(settings.private_dir / "process.lock"):
        journal = Journal(settings.private_dir / "journal.sqlite3", journal_namespace(settings))
        try:
            if args.command == "show-result":
                report = journal.report(args.message_id)
                print(
                    audit.redactor.text(report.model_dump_json(indent=2))
                    if report
                    else "未找到该消息的处理结果。"
                )
                return
            await ledger.check_schema()
            hooks = ModelAuditHooks(audit, RequestBudget())
            async with httpx.AsyncClient(
                timeout=settings.model_timeout_seconds,
                limits=httpx.Limits(max_keepalive_connections=0),
                event_hooks={"request": [hooks.request], "response": [hooks.response]},
            ) as client:
                agent = create_agent(create_model(settings, client), settings)
                channel = create_channel(settings, audit)
                reporter = CardReporter(channel, journal, audit)
                service = BookkeepingService(
                    settings, agent, ledger, knowledge, journal, audit, reporter
                )
                app = ChannelApplication(channel, service, audit)
                stop = asyncio.Event()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    asyncio.get_running_loop().add_signal_handler(sig, stop.set)
                await app.run(
                    stop,
                    lambda: print("飞书记账智能体已连接。可向机器人发送消费消息；Ctrl+C 停止。"),
                )
        finally:
            journal.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书自主记账智能体")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="启动机器人长连接")
    doctor = sub.add_parser("doctor", help="只读检查配置、分类知识与账本字段")
    doctor.add_argument("--websocket", action="store_true", help="同时验证长连接，不处理消息")
    show = sub.add_parser("show-result", help="查看本地保存的真实处理结果")
    show.add_argument("message_id")
    args = parser.parse_args()
    try:
        settings = load_settings(args.project_dir, args.env_file)
    except ValidationError as error:
        names = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        parser.exit(2, "配置无效，请检查这些字段：" + ", ".join(names) + "\n")
    audit = Audit(settings.private_dir / "events.jsonl", settings.secrets)
    try:
        asyncio.run(run(args, settings, audit))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        audit.event("startup_error", error=repr(error))
        parser.exit(1, audit.redactor.text(str(error)) + "\n")
    finally:
        audit.close()


if __name__ == "__main__":
    main()
