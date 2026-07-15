from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pplx_deep_research as pplx  # noqa: E402


def test_rejects_clarification_acknowledgement_even_with_completion_copy():
    acknowledgement = (
        "Understood. I will use exactly the requested tickers.\n\n"
        "Prepared with Deep Research using realtime financial data\n"
    ) * 10

    assert not pplx._report_looks_finished(acknowledgement)


def test_accepts_structured_report_with_multiple_sections():
    report = "\n\n".join(
        [
            "## Thesis\n" + "Evidence and analysis. " * 25,
            "## Peer comparison\n" + "Comparison detail. " * 25,
            "## Risks\n" + "Risk evidence. " * 25,
        ]
    )

    assert pplx._report_looks_finished(report)


def test_accepts_long_plain_text_fallback():
    assert pplx._report_looks_finished("Detailed research finding. " * 180)
