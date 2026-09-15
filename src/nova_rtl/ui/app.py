"""Evaluator dashboard renderers over the canonical M9 view model."""

from __future__ import annotations

from typing import Any

from nova_rtl.ui.view_models import DashboardModel


def render_text_dashboard(model: DashboardModel) -> str:
    """Render a dependency-free, evidence-linked dashboard for terminals and CI."""

    lines = [
        f"NOVA-RTL evaluator dashboard: {model.run_id}",
        f"Selected candidate: {model.selected_candidate_id}",
        f"Evidence bundle: {model.source_bundle_hash}",
    ]
    for view in model.views:
        lines.append(f"\n[{view.view_id}]")
        if not view.cards:
            lines.append("No applicable evidence claims.")
        for card in view.cards:
            evidence = ",".join(card.evidence_ids)
            lines.append(
                f"{card.title}: {card.value} | {card.authority} [{card.color}] | "
                f"evidence={evidence}"
            )
    return "\n".join(lines) + "\n"


def render_streamlit_dashboard(model: DashboardModel, streamlit: Any) -> None:
    """Render the same canonical projection through an injected Streamlit module."""

    streamlit.set_page_config(page_title="NOVA-RTL evaluator", layout="wide")
    streamlit.title("NOVA-RTL evaluator dashboard")
    streamlit.caption(
        f"Run {model.run_id} · candidate {model.selected_candidate_id} · "
        f"evidence {model.source_bundle_hash}"
    )
    for view in model.views:
        streamlit.header(view.view_id.replace("_", " ").title())
        if not view.cards:
            streamlit.info("No applicable evidence claims.")
        for card in view.cards:
            streamlit.markdown(
                f"**{card.title}:** {card.value}  \n"
                f"Authority: `{card.authority}` ({card.color})  \n"
                f"Evidence: `{', '.join(card.evidence_ids)}`"
            )


__all__ = ["render_streamlit_dashboard", "render_text_dashboard"]
