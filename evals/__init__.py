"""Developer-only evaluation harness for yucode.

This package intentionally lives outside the distributable ``yucode`` package.
Run it from a source checkout with ``python -m evals``.
"""

from .schema import EvalConfigError, SuiteSpec, TaskSpec, load_catalog, load_suite

__all__ = ["EvalConfigError", "SuiteSpec", "TaskSpec", "load_catalog", "load_suite"]
