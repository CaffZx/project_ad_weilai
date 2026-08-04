from app.llm.reasoner import render_search_term_prompt_lines


def test_prompt_marks_7d_baseline_and_only_adds_14d_for_insufficient_term():
    lines = render_search_term_prompt_lines({
        "terms": [
            {
                "keyword": "converted",
                "term_sample_insufficient": False,
                "metrics_7d": {"orders": 1, "cost": 10, "clicks": 10, "impressions": 100},
            },
            {
                "keyword": "long tail",
                "term_sample_insufficient": True,
                "metrics_7d": {"orders": 0, "cost": 3, "clicks": 4, "impressions": 80},
                "metrics_14d": {"available": True, "orders": 0, "cost": 8, "clicks": 9, "impressions": 210},
            },
        ],
    })

    text = "\n".join(lines)
    assert "[window=7d]" in text
    assert "[window=14d]" in text
    assert "sample_insufficient_7d=true" in text
    assert "sample_insufficient_7d=false" in text
    assert "ACOS(corrected)" in text
    assert text.count("[window=14d]") == 1


def test_prompt_skipped_activity_has_no_search_term_rows():
    lines = render_search_term_prompt_lines({
        "summary": {"search_term_fetch_status": "SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT"},
        "terms": [],
    })
    text = "\n".join(lines)
    assert "SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT" in text
    assert "[" not in text
