"""
Sphinx configuration for dARK Core API.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(".."))

project = "dARK Core API"
copyright = "2026, dARK Team"
author = "dARK Team"
release = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.napoleon",
    "sphinx_autodoc_typehints",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
