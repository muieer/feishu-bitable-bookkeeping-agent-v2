import asyncio
from collections.abc import Callable

from lark_channel import FeishuChannel, InboundConfig, LogLevel, PolicyConfig, SecurityConfig
from lark_channel.channel.types import InboundMessage

from .audit import Audit
from .config import Settings
from .service import BookkeepingService


def create_channel(settings: Settings, audit: Audit) -> FeishuChannel:
    policy = PolicyConfig(
        dm_policy="allowlist" if settings.user_allowlist else "open",
        allow_from=settings.user_allowlist or None,
        group_policy="open" if settings.allow_groups else "disabled",
        require_mention=True,
    )
    channel = FeishuChannel(
        app_id=settings.lark_app_id,
        app_secret=settings.lark_app_secret.get_secret_value(),
        transport="ws",
        policy=policy,
        inbound=InboundConfig(drop_self_sent=True),
        security=SecurityConfig(mode="strict"),
        log_level=LogLevel.ERROR,
    )
    audit.capture_sdk_logs()
    channel.on("error", lambda error: audit.event("channel_error", error=str(error)))
    return channel


class ChannelApplication:
    """SDK handlers run on its background loop; persist ingress on our owning loop before ACK."""

    def __init__(self, channel: FeishuChannel, service: BookkeepingService, audit: Audit):
        self.channel, self.service, self.audit = channel, service, audit
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self.accepting = True
        self.channel.on("message", self._inbound)

    async def _accept(self, msg: InboundMessage) -> None:
        if not self.accepting or msg.sender_is_bot or msg.sender_type not in {None, "user"}:
            return
        if msg.content.kind not in {"text", "post"}:
            return
        if (
            self.service.settings.user_allowlist
            and msg.sender_id not in self.service.settings.user_allowlist
        ):
            return
        text = msg.body_text or msg.content_text
        if self.service.accept(msg.message_id, msg.chat_id, text):
            self.queue.put_nowait(msg.message_id)
        else:
            self.audit.event("duplicate_delivery_ignored", message_id=msg.message_id)

    async def _inbound(self, msg: InboundMessage) -> None:
        if asyncio.get_running_loop() is self.loop:
            await self._accept(msg)
        else:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self._accept(msg), self.loop)
            )

    async def worker(self) -> None:
        while True:
            message_id = await self.queue.get()
            try:
                await self.service.process(message_id)
            except Exception as error:
                self.audit.event("worker_error", message_id=message_id, error=repr(error))
            finally:
                self.queue.task_done()

    async def notifications(self) -> None:
        while True:
            for message_id in self.service.journal.pending_notifications():
                await self.service.deliver(message_id)
            await asyncio.sleep(30)

    async def run(self, stop: asyncio.Event, ready: Callable[[], None]) -> None:
        self.service.journal.recover_interrupted()
        for message_id in self.service.journal.queued():
            self.queue.put_nowait(message_id)
        worker = notifier = None
        try:
            await self.channel.connect_until_ready(timeout=30)
            self.audit.event("channel_ready")
            ready()
            worker = asyncio.create_task(self.worker())
            notifier = asyncio.create_task(self.notifications())
            await stop.wait()
        finally:
            self.accepting = False
            if notifier:
                notifier.cancel()
                await asyncio.gather(notifier, return_exceptions=True)
            if worker:
                try:
                    async with asyncio.timeout(10):
                        await self.queue.join()
                except TimeoutError:
                    self.audit.event("shutdown_with_pending_work")
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await self.channel.disconnect()
