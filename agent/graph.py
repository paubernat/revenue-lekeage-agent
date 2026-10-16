"""The agent loop as a LangGraph state machine.

    START -> agent --(tool calls?)--> tools -> agent -> ... -> END

- State = the message history (MessagesState), persisted per thread_id by the checkpointer,
  which is what lets the agent remember earlier turns.
- `apply` / `rollback` call interrupt() inside the tools node: the graph stops, the UI shows
  Approve/Reject, and the run resumes with Command(resume="approve"|"reject").
"""
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

import config
from agent.llm import get_llm
from agent.prompts import system_prompt
from tools import ALL_TOOLS


def build_graph(checkpointer=None, llm=None):
    # One tool call per step: an interrupt re-runs the whole tools node on resume, so parallel
    # calls next to `apply` would execute twice.
    model = (llm or get_llm()).bind_tools(ALL_TOOLS, parallel_tool_calls=False)

    def agent(state: MessagesState) -> dict:
        messages = [SystemMessage(system_prompt(config.get_as_of()))] + state["messages"]
        return {"messages": [model.invoke(messages)]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition)  # -> "tools" or END
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer or MemorySaver())


def pending_interrupts(graph, thread_config: dict) -> list:
    """Interrupt payloads waiting for a human decision on this thread."""
    snapshot = graph.get_state(thread_config)
    return [i.value for task in snapshot.tasks for i in task.interrupts]
