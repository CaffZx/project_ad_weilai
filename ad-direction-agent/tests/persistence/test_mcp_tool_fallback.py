from app.data.mcp_tool_fallback import MCP_TOOL_TO_META, meta_ids_for_failed_tools


def test_meta_ids_for_failed_tools_dedup():
    failed = ["ad_product_report", "ad_keyword_report", "keyword_child_asins"]
    meta = meta_ids_for_failed_tools(failed)
    assert "META_AD_PRODUCT" in meta
    assert "META_KW_AD" in meta
    assert "META_KW_COMPETITOR_RANK" in meta
    assert len(meta) == len(set(meta))


def test_unknown_tool_ignored():
    assert meta_ids_for_failed_tools(["unknown_tool"]) == []
