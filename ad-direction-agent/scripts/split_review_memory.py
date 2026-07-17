"""将统一复盘记忆 JSON 拆分为产品级与活动级注入文件。"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


COMMON_FIELDS = (
    "source_review_case_id",
    "source_decision_id",
    "parent_asin",
    "parent_aku",
    "store_name",
    "review_window",
    "knowledge_base_version",
)


def split_review_memory(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回独立的产品级记忆和活动级记忆 JSON 对象。"""
    product_memory = payload.get("product_memory")
    activity_memories = payload.get("activity_memories")
    if not isinstance(product_memory, dict):
        raise ValueError("product_memory 必须是对象")
    if not isinstance(activity_memories, list) or not all(
        isinstance(item, dict) for item in activity_memories
    ):
        raise ValueError("activity_memories 必须是对象数组")

    common = {field: payload.get(field) for field in COMMON_FIELDS}
    schema_version = payload.get("schema_version", "1.0")
    product_output = {
        "schema_name": "ad_agent_product_memory",
        "schema_version": schema_version,
        **common,
        "memory": copy.deepcopy(product_memory),
    }
    activity_output = {
        "schema_name": "ad_agent_activity_memories",
        "schema_version": schema_version,
        **common,
        "memories": copy.deepcopy(activity_memories),
    }
    return product_output, activity_output


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("输入 JSON 顶层必须是对象")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="统一复盘记忆 JSON 文件")
    parser.add_argument("--product-output", required=True, type=Path, help="产品级输出文件")
    parser.add_argument("--activity-output", required=True, type=Path, help="活动级输出文件")
    args = parser.parse_args()

    try:
        product_output, activity_output = split_review_memory(_load_json(args.input))
        _write_json(args.product_output, product_output)
        _write_json(args.activity_output, activity_output)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    print(f"已写入产品级记忆：{args.product_output}")
    print(f"已写入活动级记忆：{args.activity_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
