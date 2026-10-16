"""Streamlit entry point: wires the UI to the LangGraph agent.  Run: streamlit run app.py"""
import os
import uuid

import streamlit as st
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.graph import build_graph, pending_interrupts
from ui.approval import render_approval
from ui.chat import example_buttons, render_history
from ui.sidebar import render_sidebar

st.set_page_config(page_title="Revenue Leakage Agent", page_icon="🕵️", layout="wide")
st.title("🕵️ Revenue Leakage Agent")
st.caption("Investigates billing plans vs invoices, proposes fixes, and applies them to the sandbox "
           "only after your approval.")

if not os.getenv("ANTHROPIC_API_KEY"):
    st.error("ANTHROPIC_API_KEY is not set. Copy `.env.example` to `.env` and add your key.")
    st.stop()


def new_conversation() -> None:
    st.session_state.thread_id = str(uuid.uuid4())


if "graph" not in st.session_state:
    st.session_state.graph = build_graph()  # holds its own MemorySaver = this session's memory
    new_conversation()

graph = st.session_state.graph
thread = {"configurable": {"thread_id": st.session_state.thread_id}}


def run(graph_input) -> None:
    try:
        with st.spinner("Thinking…"):
            graph.invoke(graph_input, thread)
    except Exception as e:  # surface API/config errors in the chat instead of crashing
        st.session_state.error = f"{type(e).__name__}: {e}"


def decide(decision: str) -> None:
    run(Command(resume=decision))


messages = graph.get_state(thread).values.get("messages", [])
render_sidebar(new_conversation, messages)

render_history(messages)
if err := st.session_state.pop("error", None):
    st.error(err)

pending = pending_interrupts(graph, thread)
if pending:
    render_approval(pending[0], decide)

prompt = None if messages or pending else example_buttons()
typed = st.chat_input("Approve or reject the action above first" if pending else "Ask about plans, invoices, leakage…",
                      disabled=bool(pending))
prompt = typed or prompt
if prompt:
    st.chat_message("user").markdown(prompt)
    run({"messages": [HumanMessage(prompt)]})
    st.rerun()
