from app.data import decision_config_reader as reader


def test_agent_config_row_only_contains_strategy_layer_fields():
    config = reader.agent_config_row_to_layer14({
        "product_position": "P1_PRODUCT",
        "operating_mode": "CLEARANCE_ONLY",
        "season_type": "peak",
        "advert_purposes": "[\"CONVERSION\"]",
        "target_keyword_types": "[\"LONG_TAIL\"]",
        "advert_direction_types": "[\"PUSH_NATURAL\"]",
        "target_acos_suggest": 25,
        "daily_budget_suggest": 12.5,
        "day_range": "DAY_7",
    })

    assert set(config) == {"product_level", "operating_mode", "season_stage"}
    assert config["product_level"]
