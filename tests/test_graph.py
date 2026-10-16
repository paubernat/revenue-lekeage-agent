"""The human-in-the-loop guarantee, tested with a scripted fake LLM (no API key needed)."""
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command

from agent.graph import build_graph, pending_interrupts
from core import repository

SEPT = "F-C-1001-missing_period-2025-09-01"


class ScriptedLLM(BaseChatModel):
    script: list

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name, args, id_):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_}])


def run(decision):
    llm = ScriptedLLM(script=[
        call("propose_make_good_invoice", {"plan_id": "C-1001", "amount": 8000,
                                           "reason": "Missing September", "finding_id": SEPT}, "1"),
        call("apply", {"action_draft": "P-001"}, "2"),
        AIMessage(content="done"),
    ])
    graph = build_graph(llm=llm)
    cfg = {"configurable": {"thread_id": "t"}}
    graph.invoke({"messages": [HumanMessage("make good September and apply it")]}, cfg)

    pending = pending_interrupts(graph, cfg)
    assert pending and pending[0]["proposal"]["proposal_id"] == "P-001"
    assert repository.read_sandbox("invoices.json") == []  # paused: nothing written yet

    graph.invoke(Command(resume=decision), cfg)
    assert pending_interrupts(graph, cfg) == []
    return graph.get_state(cfg).values["messages"]


def test_apply_waits_for_human_and_approve_writes():
    run("approve")
    assert [i["invoice_id"] for i in repository.read_sandbox("invoices.json")] == ["INV-MG-001"]


def test_reject_writes_nothing():
    run("reject")
    assert repository.read_sandbox("invoices.json") == []
    assert repository.read_sandbox("proposals.json")[0]["status"] == "rejected"
