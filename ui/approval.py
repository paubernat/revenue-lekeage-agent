"""Approval card shown while the graph is paused on an interrupt from apply/rollback."""
from typing import Callable

import streamlit as st

LABELS = {"make_good_invoice": "Make-good invoice", "credit_memo": "Credit memo", "plan_amendment": "Plan amendment"}


def render_approval(payload: dict, on_decision: Callable[[str], None]) -> None:
    with st.chat_message("assistant"):
        with st.container(border=True):
            if payload["type"] == "apply":
                p = payload["proposal"]
                st.markdown(f"#### ⏸ Approval required: apply **{LABELS[p['kind']]}** `{p['proposal_id']}`")
                st.markdown(f"**Reason:** {p['reason']}")
                if p.get("finding_id"):
                    st.markdown(f"**Finding:** `{p['finding_id']}`")
                body = p["payload"]
                if p["kind"] == "make_good_invoice":
                    st.metric(f"Invoice to {body['customer_name']} ({body['plan_id']})",
                              f"{body['amount_invoiced']:,.2f} {body['currency']}")
                elif p["kind"] == "credit_memo":
                    st.metric(f"Credit against {body['invoice_id']} ({body['plan_id']})",
                              f"-{body['amount']:,.2f} {body['currency']}")
                else:
                    st.markdown(f"**Plan:** `{body['plan_id']}`")
                    st.table([{"field": k, "before": str(body["previous"].get(k)), "after": str(v)}
                              for k, v in body["change_set"].items()])
                for w in p.get("warnings", []):
                    st.warning(w)
                with st.expander("Record that will be written to the sandbox"):
                    st.json(body)
            else:
                a = payload["action"]
                st.markdown(f"#### ⏸ Approval required: roll back `{a['action_id']}`")
                st.markdown(f"Removes **{a['record_id']}** ({a['kind']}) from `sandbox/{a['ledger']}`.")

            col1, col2 = st.columns(2)
            col1.button("✅ Approve", type="primary", use_container_width=True,
                        on_click=on_decision, args=("approve",))
            col2.button("❌ Reject", use_container_width=True, on_click=on_decision, args=("reject",))
