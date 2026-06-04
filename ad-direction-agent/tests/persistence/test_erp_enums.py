"""ERP enum mapping unit tests."""
from app.persistence.erp_writer.text_utils import (
    map_ad_purpose,
    map_campaign_group_type,
    map_direction_type,
    map_direction_types_json,
    map_product_position,
    map_product_stage,
    map_purpose_target,
    map_season_type,
    map_target_keyword_type,
    normalize_site_code,
    to_enum_list,
)


def test_site_code_normalize():
    assert normalize_site_code(None) == "Amazon_US"
    assert normalize_site_code("US") == "Amazon_US"
    assert normalize_site_code("Amazon_DE") == "Amazon_DE"
    assert normalize_site_code("UK") == "Amazon_UK"


def test_product_enums():
    assert map_product_position("腰部") == "WAIST"
    assert map_product_stage("推进期") == "PROMOTING"
    assert map_season_type("旺季准备") == "PEAK_SEASON_PREPARE"


def test_ad_purposes_json():
    import json

    raw = to_enum_list(["转化型", "排名型"], map_ad_purpose)
    assert json.loads(raw) == ["CONVERSION", "RANKING"]
    assert map_purpose_target("Conversion") == "CONVERSION"


def test_direction_types_json():
    import json

    raw = map_direction_types_json(["push_natural", "optimize_acos"])
    assert json.loads(raw) == ["PUSH_NATURAL", "OPTIMIZE_ACOS"]
    assert map_direction_type("balance_maintain") == "BALANCE_MAINTAIN"
    assert map_direction_type("ADD_KEYWORD_EXPANSION") == "EXPAND_KEYWORDS"


def test_campaign_group_type_map():
    assert map_campaign_group_type("主推") == "core"
    assert map_campaign_group_type("广泛/自动") == "auto_broad"
    assert map_campaign_group_type("测试/新增") == "test"
    assert map_campaign_group_type("淘汰") == "eliminate"
    assert map_campaign_group_type("core") == "core"
    assert map_campaign_group_type(None) is None


def test_target_keyword_types_json():
    import json

    raw = to_enum_list(["大词", "长尾词"], map_target_keyword_type)
    assert json.loads(raw) == ["GENERIC", "LONG_TAIL"]
    assert map_target_keyword_type("竞品词") == "COMPETITOR"
