"""Data providers for the interactive research app.

Each provider is stdlib-only (urllib) and returns plain dicts so the rest of the
app stays dependency-free. The guiding rule of this repo applies here too: never
trust a single unverified number, so multiple providers feed the same metrics and
``research_pull`` cross-checks them.
"""

from . import fred, sec_edgar, yahoo  # noqa: F401

__all__ = ["yahoo", "sec_edgar", "fred"]
