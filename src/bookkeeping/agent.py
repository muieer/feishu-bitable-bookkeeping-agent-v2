"""LangChain harness: autonomous tool loop with bounded, validated completion."""

from collections.abc import Awaitable, Callable
from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.agents import create_agent as langchain_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ToolCallRequest, hook_config
from langchain.agents.structured_output import ToolStrategy
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import ValidationError

from .config import Settings
from .domain import Decision, ExpenseProposal
from .prompts import SYSTEM_PROMPT
from .tools import RunDeps, ToolInputError, validate_decision
from .tools import add_expenses as submit_expenses
from .tools import query_history as lookup_history


@tool
async def query_history(runtime: ToolRuntime[Any], keyword: str, limit: int = 20) -> dict:
    """查询指定账本中消费详情包含关键词的有限历史样本，辅助分类；最多 50 条，不用于去重。"""
    return await lookup_history(runtime.context, keyword, limit)


@tool
async def add_expenses(runtime: ToolRuntime[Any], expenses: list[ExpenseProposal]) -> list[dict]:
    """向唯一指定账本新增一笔或多笔已确认消费；逐笔校验、独立执行并返回真实状态。"""
    return await submit_expenses(runtime.context, expenses)


class HarnessState(AgentState[Decision]):
    model_requests: NotRequired[int]
    output_retries: NotRequired[int]


def model_tool_calls(output: AIMessage) -> list[dict]:
    """Include malformed calls and retain provider order for exhaustive output handling."""
    calls = [*output.tool_calls, *output.invalid_tool_calls]
    positions = {
        call["id"]: index
        for index, call in enumerate(output.additional_kwargs.get("tool_calls", []))
    }
    return sorted(calls, key=lambda call: positions.get(call["id"], len(positions)))


def tool_failure(deps: RunDeps, name: str) -> None:
    """The baseline allows two failed rounds per tool, rather than two failed calls."""
    if deps.tool_retries.get(name, 0) >= 2:
        label = "最终结果" if name == "Decision" else "工具参数"
        raise RuntimeError(f"{label}校验失败次数已达到上限，已停止本次处理")
    deps.failed_tools.add(name)


class BookkeepingHarness(AgentMiddleware[HarnessState, RunDeps]):
    state_schema = HarnessState

    def __init__(self, settings: Settings | None):
        self.model_settings = {
            "parallel_tool_calls": False,
            "max_tokens": settings.model_output_token_limit if settings else 16384,
            "reasoning_effort": settings.ark_reasoning_effort if settings else "low",
        }
        self.tool_schemas = {
            registered.name: registered.tool_call_schema
            for registered in (query_history, add_expenses)
        }

    async def abefore_model(self, state: HarnessState, runtime: Runtime[RunDeps]) -> dict:
        deps = runtime.context
        requests = state.get("model_requests", 0)
        if requests >= deps.request_limit:
            raise RuntimeError("已达到本条消息的模型请求上限")
        deps.tool_retries = {name: deps.tool_retries.get(name, 0) + 1 for name in deps.failed_tools}
        deps.failed_tools.clear()
        return {"model_requests": requests + 1}

    async def awrap_model_call(self, request: ModelRequest, handler):
        deps = request.runtime.context
        context = (
            f"任务执行日：{deps.today.isoformat()}（Asia/Shanghai）。\n"
            "当前个人分类知识如下：\n" + deps.knowledge.context
        )
        return await handler(
            request.override(
                system_message=SystemMessage(SYSTEM_PROMPT + "\n\n" + context),
                model_settings={**request.model_settings, **self.model_settings},
            )
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        deps = request.runtime.context
        # LangGraph may dispatch several calls from one response concurrently.
        async with deps.tool_lock:
            try:
                result = await handler(request)
            except ToolInputError as error:
                tool_failure(deps, request.tool_call["name"])
                return ToolMessage(
                    content=deps.audit.redactor.text(str(error)),
                    name=request.tool_call["name"],
                    tool_call_id=request.tool_call["id"],
                    status="error",
                )
            if isinstance(result, ToolMessage) and result.status == "error":
                tool_failure(deps, request.tool_call["name"])
                # Framework validation messages can contain raw inputs, including secrets.
                result.content = "工具参数不符合格式或数量限制，请按工具定义修正尚未提交的消费"
            else:
                deps.tool_calls += 1
            return result

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: HarnessState, runtime: Runtime[RunDeps]) -> dict:
        deps = runtime.context
        messages = state["messages"]
        index = next(
            i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], AIMessage)
        )
        calls = model_tool_calls(messages[index])
        retries = state.get("output_retries", 0)
        if not calls:
            if retries >= 2:
                raise RuntimeError("最终结果校验失败次数已达到上限，已停止本次处理")
            return {
                "structured_response": None,
                "output_retries": retries + 1,
                "messages": [HumanMessage(content="请按 Decision 格式返回真实结果")],
                "jump_to": "model",
            }

        previous = {
            message.tool_call_id: message
            for message in messages[index + 1 :]
            if isinstance(message, ToolMessage)
        }
        receipts = []

        def receipt(call: dict, content: str, *, error: bool) -> ToolMessage:
            values = {"content": content, "status": "error" if error else "success"}
            if existing := previous.get(call["id"]):
                return existing.model_copy(update=values)
            return ToolMessage(name=call["name"], tool_call_id=call["id"], **values)

        # Preserve exhaustive baseline semantics: validate outputs against the journal
        # before this response's ledger tools, and retain the first valid output.
        decision = None
        for call in (call for call in calls if call["name"] == "Decision"):
            try:
                if call["type"] == "invalid_tool_call":
                    raise ValueError("最终结果参数不是完整合法的 JSON，请修正后重新返回")
                candidate = Decision.model_validate(call["args"])
                validate_decision(deps, candidate)
            except ValueError as error:
                try:
                    tool_failure(deps, "Decision")
                except RuntimeError:
                    if decision is None:
                        raise
                reason = (
                    "最终结果格式不符合 Decision 定义，请修正后重新返回"
                    if isinstance(error, ValidationError)
                    else deps.audit.redactor.text(str(error))
                )
                deps.audit.event("decision_retry", reason=reason)
                receipts.append(receipt(call, reason, error=True))
                retries += 1
            else:
                if decision is None:
                    decision = candidate
                receipts.append(receipt(call, "最终结果已通过校验", error=False))

        business_calls = [call for call in calls if call["name"] != "Decision"]
        if deps.tool_calls + len(business_calls) > 30:
            raise RuntimeError("已达到本条消息的工具调用上限")

        # Validate every call before dispatch, as the baseline did. A fatal error
        # must not leave a valid sibling already submitted to the ledger.
        pending = False
        for call in business_calls:
            try:
                if call["type"] == "invalid_tool_call" or call["name"] not in self.tool_schemas:
                    raise ValueError("工具名称或 JSON 参数不合法")
                schema = self.tool_schemas[call["name"]]
                if call["args"].keys() - schema.model_fields.keys():
                    raise ValueError("工具参数包含未定义的字段")
                schema.model_validate(call["args"])
            except ValueError:
                tool_failure(deps, call["name"])
                receipts.append(
                    receipt(
                        call,
                        "工具参数不符合格式或数量限制，请按工具定义修正尚未提交的消费",
                        error=True,
                    )
                )
            else:
                pending = True

        update = {
            "structured_response": decision,
            "output_retries": retries,
            "messages": receipts,
        }
        if decision is None and not pending:
            update["jump_to"] = "model"
        return update


def create_agent(model: BaseChatModel, settings: Settings | None = None):
    return langchain_agent(
        model,
        tools=[query_history, add_expenses],
        middleware=[BookkeepingHarness(settings)],
        response_format=ToolStrategy(
            Decision,
            handle_errors="最终结果格式不符合 Decision 定义，请修正后重新返回",
            tool_message_content="最终结果已提交给程序校验",
        ),
        context_schema=RunDeps,
        name="bookkeeping",
    )
