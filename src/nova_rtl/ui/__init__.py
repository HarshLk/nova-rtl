"""Typed evaluator projections and local demo UI."""

from nova_rtl.ui.app import render_streamlit_dashboard, render_text_dashboard
from nova_rtl.ui.view_models import build_view_model, build_view_model_from_replay

__all__ = [
    "build_view_model",
    "build_view_model_from_replay",
    "render_streamlit_dashboard",
    "render_text_dashboard",
]
