"""Workflow graph exports kept lazy to avoid agent/workflow import cycles."""

from importlib import import_module

__all__ = ["build_pipeline_graph", "pipeline_graph", "travel_graph"]


def __getattr__(name):
    if name in {"build_pipeline_graph", "pipeline_graph"}:
        module = import_module("backend.workflow.pipeline")
        return getattr(module, name)
    if name == "travel_graph":
        return import_module("backend.workflow.travel_graph").travel_graph
    raise AttributeError(name)
