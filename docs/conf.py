"""Sphinx configuration for the yucode documentation."""

from __future__ import annotations

import tomllib
from pathlib import Path

_pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8"))
_meta = _pyproject["project"]

# -- Project information -----------------------------------------------------

project = "yucode"
author = _meta["authors"][0]["name"]
copyright = f"{author}"
release = _meta["version"]
version = ".".join(release.split(".")[:2])

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx_copybutton",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
root_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "learning"]
templates_path = ["_templates"]

myst_enable_extensions = ["colon_fence", "deflist", "linkify", "substitution"]
myst_heading_anchors = 3

# 中文标题无法生成稳定的标题锚点(默认 slug 会剥离汉字),被跨页链接引用的标题
# 通过显式目标 `(label)=` 固定 id。MyST 的静态 xref 检查不识别这些显式目标,
# 会误报 myst.xref_missing,但渲染出的链接与 id 真实存在(已验证),故压制该检查。
suppress_warnings = ["myst.xref_missing"]

# -- HTML output -------------------------------------------------------------

import better  # noqa: E402

html_theme = "better"
html_theme_path = [better.better_theme_path]
html_theme_options = {
    "showheader": False,
    "showrelbartop": False,
    "showrelbarbottom": True,
    "sidebarwidth": "16rem",
    # Show every page's major sections while keeping third-level headings out
    # of the global navigation.
    "globaltoc_collapse": False,
    "globaltoc_includehidden": True,
    "globaltoc_maxdepth": 2,
}
html_sidebars = {
    # The global TOC shows the current page's sections, so a separate local TOC
    # would duplicate them.
    "**": ["globaltoc.html", "searchbox.html"],
}
html_title = f"{project} {release}"
html_short_title = project
html_static_path = ["_static"]
# Loaded via html_css_files (not the theme's cssfiles option) so Sphinx appends a
# ?v=<checksum> cache buster; without it browsers keep a stale custom.css.
html_css_files = ["custom.css"]
html_js_files = ["scrollspy.js"]
html_show_sphinx = False
html_copy_source = False
html_show_sourcelink = False
pygments_style = "one-dark"

# -- Extension tuning --------------------------------------------------------

copybutton_prompt_text = r"\$ |>>> "

copybutton_prompt_is_regexp = True
