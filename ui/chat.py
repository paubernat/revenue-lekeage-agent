"""Renders the conversation straight from the graph state (the single source of truth),
with each tool call and its result in an expander as the reasoning trace."""
import json

import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

EXAMPLES = [
    "Check plan C-1001 for revenue leakage",
    "Investigate all plans and summarise the leakage",
    "What's the difference between a credit memo and a make-good invoice?",
]


def text_of(message) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(b.get("text", "") for b in message.content if isinstance(b, dict) and b.get("type") == "text")


def _parse(content):
    try:
        return json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        return content


def render_history(messages: list) -> None:
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
    for m in messages:
        if isinstance(m, HumanMessage):
            st.chat_message("user").markdown(text_of(m))
        elif isinstance(m, AIMessage):
            with st.chat_message("assistant"):
                if text := text_of(m):
                    st.markdown(text)
                for call in m.tool_calls:
                    args = ", ".join(f"{k}={v!r}" for k, v in call["args"].items())
                    with st.expander(f"🔧 {call['name']}({args[:80]}{'…' if len(args) > 80 else ''})"):
                        st.caption("arguments")
                        st.json(call["args"])
                        if call["id"] in results:
                            st.caption("result")
                            result = _parse(results[call["id"]].content)
                            st.json(result) if isinstance(result, (dict, list)) else st.code(result)
                        else:
                            st.caption("waiting for human decision…")


def example_buttons() -> str | None:
    st.caption("Try:")
    cols = st.columns(len(EXAMPLES))
    for col, example in zip(cols, EXAMPLES):
        if col.button(example, use_container_width=True):
            return example
    return None
