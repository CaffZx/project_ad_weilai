from unittest.mock import Mock

from app.data.mcp_adapter import McpAdapter
from app.data.mcp_mapping import McpContext
from app.models.asin_data import ASINData
from app.persistence.mysql_state_manager import MySQLStateManager
from app.workflow.steps.tactics import _attach_data_identity


def _manager():
    mgr = MySQLStateManager()
    mgr.ensure_schema = lambda: None  # type: ignore[method-assign]
    return mgr


def test_long_term_config_writes_product_identity_columns():
    mgr = _manager()
    mgr.get_long_term_config = Mock(return_value={})  # type: ignore[method-assign]
    mgr._execute = Mock()

    assert mgr.set_long_term_config(
        "B0TEST",
        {
            "shop_id": 1622,
            "parent_seller_sku": "SKU-001",
            "product_level": "P2",
            "product_stage": "TEST",
            "season_stage": "OFF",
        },
    )

    sql, params = mgr._execute.call_args_list[0].args[:2]
    assert "shop_id" in sql
    assert "parent_seller_sku" in sql
    assert params[:3] == ("B0TEST", 1622, "SKU-001")


def test_long_term_config_writes_nullable_operating_mode_column():
    """经营模式与三项战略字段同表持久化，空值必须原样写为 SQL NULL。"""
    mgr = _manager()
    mgr.get_long_term_config = Mock(return_value={})  # type: ignore[method-assign]
    mgr._execute = Mock()

    assert mgr.set_long_term_config(
        "B0TEST",
        {
            "product_level": "常规产品 (P2)",
            "operating_mode": None,
        },
    )

    sql, params = mgr._execute.call_args_list[0].args[:2]
    assert "operating_mode" in sql
    assert params[-2] is None


def test_workflow_state_writes_product_identity_columns():
    mgr = _manager()
    mgr._execute = Mock()

    assert mgr.set_workflow_state(
        "B0TEST",
        {
            "shop_id": 1622,
            "parent_seller_sku": "SKU-001",
            "current_layer": "execution",
            "layers_completed": ["strategy"],
        },
    )

    sql, params = mgr._execute.call_args_list[0].args[:2]
    assert "shop_id" in sql
    assert "parent_seller_sku" in sql
    assert params[:3] == ("B0TEST", 1622, "SKU-001")


def test_analysis_session_writes_product_identity_columns():
    mgr = _manager()
    mgr._execute = Mock()

    assert mgr.set_analysis_session(
        "B0TEST",
        "RUN1",
        shop_id=1622,
        parent_seller_sku="SKU-001",
    )

    sql, params = mgr._execute.call_args.args[:2]
    assert "shop_id" in sql
    assert "parent_seller_sku" in sql
    assert params[:3] == ("B0TEST", 1622, "SKU-001")


def test_clear_analysis_execution_started_reports_failure():
    mgr = _manager()
    mgr._execute = Mock(side_effect=RuntimeError("db down"))

    assert mgr.clear_analysis_execution_started("B0TEST") is False


def test_mcp_adapter_carries_context_identity_into_asin_data():
    ctx = McpContext(
        parent_asin="B0TEST",
        parent_seller_sku="SKU-001",
        shop_account="shop-us",
        shop_id=1622,
        site_code="Amazon_US",
        start_date="2026-07-01",
        end_date="2026-07-07",
    )

    data = McpAdapter().assemble_from_payloads(
        asin="B0TEST",
        ctx=ctx,
        payload_map={},
        missing_fields=[],
        days=7,
    )

    assert data.asin == "B0TEST"
    assert data.parent_asin == "B0TEST"
    assert data.shop_id == 1622
    assert data.parent_seller_sku == "SKU-001"


def test_attach_data_identity_adds_same_source_identity_to_workflow_state():
    data = ASINData(
        asin="B0TEST",
        parent_asin="B0TEST",
        shop_id=1622,
        parent_seller_sku="SKU-001",
    )
    wf = {"current_layer": "tactics"}

    _attach_data_identity(wf, data)

    assert wf["shop_id"] == 1622
    assert wf["parent_seller_sku"] == "SKU-001"
