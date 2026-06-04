"""Tab4 direction_recommend_detail content_json (display_* fields)."""
from __future__ import annotations

import json

from app.persistence.erp_writer.auto_push import _merge_decision_meta
from app.persistence.erp_writer.text_utils import (
    build_wizard_direction_content_json,
    normalize_advert_direction_types_list,
)

B0CGH9_DIRECTIONS = [
    {
        "id": "push_natural",
        "label": "推进自然位",
        "suitability_score": 5.0,
        "suitability": "not_recommended",
        "reason": "根据当前数据，判断暂不适合将推自然位作为主方向",
    },
    {
        "id": "optimize_acos",
        "label": "优化 ACOS",
        "suitability_score": 0.0,
        "suitability": "not_recommended",
        "reason": "根据ACOS 指标，判断处于合理区间，可按日常节奏微调",
    },
]


def test_b0cgh9_scores_and_tags():
    for d in B0CGH9_DIRECTIONS:
        body = build_wizard_direction_content_json(d, {}, None)
        assert body["display_label"] == d["label"]
        assert body["display_reason"] == d["reason"]
        assert body["display_score"] == d["suitability_score"]
        assert body["display_tag"] == d["suitability"]
        assert body["display_tag_zh"] in ("暂不建议", "可考虑", "推荐")


def test_decision_package_tasks_flattened():
    d = B0CGH9_DIRECTIONS[1]
    pkg = {
        "direction": "优化 ACOS",
        "tasks": [
            {
                "priority": "high",
                "action": "否定低效词",
                "details": "分析搜索词报告",
                "estimated_impact": "降低 ACOS",
            }
        ],
    }
    body = build_wizard_direction_content_json(d, {"asin": "B0CGH9QRKK"}, pkg)
    assert len(body["display_tasks"]) == 1
    assert body["display_tasks"][0]["action"] == "否定低效词"
    assert body["decision_package"]["tasks"][0]["action"] == "否定低效词"


def test_merge_decision_meta_normalizes_legacy_codes():
    meta = _merge_decision_meta(
        {},
        {
            "decision_meta": {
                "advert_direction_types": [
                    "OPTIMIZE_ACOS",
                    "BALANCE_MAINTENANCE",
                    "ADD_KEYWORD_EXPANSION",
                ]
            }
        },
    )
    assert meta["advert_direction_types"] == [
        "OPTIMIZE_ACOS",
        "BALANCE_MAINTAIN",
        "EXPAND_KEYWORDS",
    ]


def test_content_json_serializable():
    body = build_wizard_direction_content_json(B0CGH9_DIRECTIONS[0], {}, None)
    raw = json.dumps(body, ensure_ascii=False)
    parsed = json.loads(raw)
    assert parsed["display_label"] == "推进自然位"
    assert "display_label" in raw
