from lark_channel.channel.types import Conversation, Identity, InboundMessage, TextContent

from bookkeeping.channel import ChannelApplication, create_channel


def test_real_channel_sdk_configuration_constructs(settings, audit):
    channel = create_channel(settings, audit)
    assert channel._config.transport.kind == "ws"
    assert channel._config.policy.group_policy == "disabled"
    assert channel._config.security.mode == "strict"


async def test_ingress_is_durable_before_callback_returns_and_duplicates_are_not_queued(
    settings, audit, journal, knowledge
):
    from bookkeeping.agent import create_agent
    from bookkeeping.service import BookkeepingService

    from .support import MemoryLedger, ScriptedModel

    service = BookkeepingService(
        settings, create_agent(ScriptedModel([]).model), MemoryLedger(), knowledge, journal, audit
    )
    channel = create_channel(settings, audit)
    app = ChannelApplication(channel, service, audit)
    message = InboundMessage(
        id="omIncoming",
        create_time=0,
        conversation=Conversation("chat", "p2p"),
        sender=Identity("ouUser", sender_type="user"),
        content=TextContent(text="买咖啡18元"),
        content_text="买咖啡18元",
    )
    await app._inbound(message)
    assert journal.message(message.id).state == "queued"
    assert app.queue.qsize() == 1
    await app._inbound(message)
    assert app.queue.qsize() == 1
