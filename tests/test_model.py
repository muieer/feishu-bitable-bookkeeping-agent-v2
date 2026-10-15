"""Exercise the real LangChain/OpenAI adapter through a local mock HTTP transport."""

import json

import httpx
import pytest

from bookkeeping.agent import create_agent
from bookkeeping.audit import ModelAuditHooks, RequestBudget
from bookkeeping.model import create_model
from bookkeeping.service import BookkeepingService

from .support import TODAY, MemoryLedger, proposal


def completion(name: str, arguments: dict, *, reasoning: str = "private-test-reasoning") -> dict:
    return {
        "id": "chatcmpl-offline-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning,
                    "tool_calls": [
                        {
                            "id": "call-" + name,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


async def test_ark_request_contract_reasoning_roundtrip_and_journal_result(
    settings, audit, journal, knowledge
):
    requests = []

    async def transport(request):
        requests.append(json.loads(request.content))
        body = (
            completion("add_expenses", {"expenses": [proposal().model_dump()]})
            if len(requests) == 1
            else completion("Decision", {})
        )
        return httpx.Response(200, json=body)

    budget = RequestBudget(10)
    hooks = ModelAuditHooks(audit, budget)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        event_hooks={"request": [hooks.request], "response": [hooks.response]},
    ) as client:
        model = create_model(settings, client)
        ledger = MemoryLedger()
        service = BookkeepingService(
            settings, create_agent(model, settings), ledger, knowledge, journal, audit
        )
        report = await service.handle("http-contract", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice is None and len(report.entries) == len(ledger.created) == 1
    assert report.entries[0].outcome.status == "success"
    assert len(requests) == budget.count == 2
    for body in requests:
        assert body["model"] == settings.ark_model
        assert body["reasoning_effort"] == settings.ark_reasoning_effort
        assert body["max_tokens"] == settings.model_output_token_limit
        assert body["parallel_tool_calls"] is False and body["stream"] is False
        assert "max_completion_tokens" not in body
        tools = {item["function"]["name"]: item["function"] for item in body["tools"]}
        assert set(tools) == {"query_history", "add_expenses", "Decision"}
        assert set(tools["query_history"]["parameters"]["properties"]) == {"keyword", "limit"}
        assert set(tools["add_expenses"]["parameters"]["properties"]) == {"expenses"}
    assistant = next(item for item in requests[1]["messages"] if item["role"] == "assistant")
    assert assistant["reasoning_content"] == "private-test-reasoning"
    events = [
        json.loads(line)
        for line in (settings.private_dir / "events.jsonl").read_text().splitlines()
    ]
    assert sum(event["event"] == "model_request" for event in events) == 2
    assert sum(event["event"] == "model_response" for event in events) == 2
    finished = next(event for event in events if event["event"] == "agent_completed")
    assert len(finished["usage"]) == 2 and finished["usage"][0]["total_tokens"] == 120
    assert all(secret not in json.dumps(events) for secret in settings.secrets)


@pytest.mark.parametrize("status", [429, 500])
async def test_sdk_does_not_retry_http_failures(settings, audit, journal, knowledge, status):
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "offline injected error"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        model = create_model(settings, client)
        service = BookkeepingService(
            settings, create_agent(model, settings), MemoryLedger(), knowledge, journal, audit
        )
        report = await service.handle("http-error", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice and report.entries == [] and len(requests) == 1


async def test_network_budget_blocks_next_request_but_keeps_confirmed_writes(
    settings, audit, journal, knowledge
):
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(
            200, json=completion("add_expenses", {"expenses": [proposal().model_dump()]})
        )

    budget = RequestBudget(1)
    hooks = ModelAuditHooks(audit, budget)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        event_hooks={"request": [hooks.request], "response": [hooks.response]},
    ) as client:
        ledger = MemoryLedger()
        service = BookkeepingService(
            settings,
            create_agent(create_model(settings, client), settings),
            ledger,
            knowledge,
            journal,
            audit,
        )
        report = await service.handle("http-limit", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice and len(report.entries) == len(ledger.created) == 1
    assert report.entries[0].outcome.status == "success"
    assert len(requests) == budget.count == 1


@pytest.mark.parametrize("name", ["add_expenses", "Decision"])
@pytest.mark.parametrize("bad_rounds", [1, 2, 3])
@pytest.mark.parametrize("include_peer", [False, True])
async def test_malformed_call_with_valid_peer_obeys_original_retry_limit(
    settings, audit, journal, knowledge, name, bad_rounds, include_peer
):
    requests = []

    async def transport(request):
        requests.append(json.loads(request.content))
        index = len(requests)
        if index > bad_rounds:
            return httpx.Response(200, json=completion("Decision", {"no_consumption": True}))
        body = completion(name, {})
        calls = body["choices"][0]["message"]["tool_calls"]
        calls[0]["id"] = f"malformed-{index}"
        calls[0]["function"]["arguments"] = '{"invalid":'
        if include_peer:
            calls.append(
                {
                    "id": f"valid-{index}",
                    "type": "function",
                    "function": {"name": "query_history", "arguments": '{"keyword":"咖啡"}'},
                }
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        ledger = MemoryLedger()
        service = BookkeepingService(
            settings,
            create_agent(create_model(settings, client), settings),
            ledger,
            knowledge,
            journal,
            audit,
        )
        report = await service.handle("malformed-peer", "chat", "今天心情不错", today=TODAY)
    assert bool(report.notice) == (bad_rounds == 3) and report.entries == []
    assert report.no_consumption == (bad_rounds < 3)
    assert len(requests) == min(bad_rounds + 1, 3)
    assert len(ledger.queries) == (min(bad_rounds, 2) if include_peer else 0)
    for body in requests[1:]:
        pending = set()
        for message in body["messages"]:
            if message["role"] == "assistant":
                assert not pending
                pending.update(call["id"] for call in message.get("tool_calls", []))
            elif message["role"] == "tool":
                pending.remove(message["tool_call_id"])
            else:
                assert not pending
        assert not pending
