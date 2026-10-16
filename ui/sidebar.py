"""Settings and the audit trail: as-of date, sandbox reset, proposals, actions, audit log."""
from typing import Callable

import streamlit as st

import config
from core import repository


def render_sidebar(on_new_conversation: Callable[[], None]) -> None:
    with st.sidebar:
        st.header("⚙️ Settings")
        as_of = st.date_input("Analysis as-of date", value=config.get_as_of(),
                              help="Only billing periods that started on or before this date count as due.")
        config.set_as_of(as_of)
        st.caption(f"Model: `{config.MODEL}`")

        col1, col2 = st.columns(2)
        if col1.button("New chat", use_container_width=True):
            on_new_conversation()
        if col2.button("Reset sandbox", use_container_width=True,
                       help="Empties every sandbox ledger and starts a new chat"):
            repository.reset_sandbox()
            on_new_conversation()

        st.divider()
        proposals = repository.read_sandbox("proposals.json")
        st.subheader(f"📝 Proposals ({len(proposals)})")
        for p in reversed(proposals):
            icon = {"pending": "🟡", "approved": "🟢", "rejected": "⚪", "rolled_back": "↩️"}[p["status"]]
            amount = p["payload"].get("amount_invoiced", p["payload"].get("amount", ""))
            st.markdown(f"{icon} `{p['proposal_id']}` {p['kind']} {amount} · *{p['status']}*")

        actions = repository.read_sandbox("actions.json")
        st.subheader(f"✅ Applied actions ({len(actions)})")
        for a in reversed(actions):
            st.markdown(f"`{a['action_id']}` → `{a['record_id']}` · *{a['status']}*")

        with st.expander("📜 Audit log (sandbox/audit_log.json)"):
            st.json(list(reversed(repository.read_sandbox("audit_log.json"))))
