from app.core.campaign_sample import assess_campaign_sample


def test_campaign_sample_insufficient_uses_kb17_boundaries():
    assert assess_campaign_sample(days_online=2, clicks_7d=20, cost_7d=100, target_cpa=20).insufficient
    assert assess_campaign_sample(days_online=3, clicks_7d=9, cost_7d=100, target_cpa=20).insufficient
    assert not assess_campaign_sample(days_online=3, clicks_7d=10, cost_7d=100, target_cpa=20).insufficient


def test_campaign_sample_unknown_values_do_not_become_new_or_zero():
    result = assess_campaign_sample(days_online=-1, clicks_7d=None, cost_7d=None, target_cpa=None)
    assert result.insufficient is False
    assert result.data_missing
