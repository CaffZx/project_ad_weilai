"""KB 切片注入回归测试（2026-06-24 治注意力稀释）。

锁住 campaign 三处 LLM 调用的 KB 切片成果，并补上 kb_loader 此前的零测试：
- 切块器按 ## 编号标题正确切节（含 7A 这类后缀节号）
- 精准/广泛/新增 三 preset 体量大幅压缩、不回退整文件
- 关键预算/容忍度规则零丢失（防误删核心）
- 噪声节（组合回算/开发流程/ERP输出schema/match_type错配）确实剔除
- 缺节告警（迭代护栏：KB 改版重编号/写错节号 → 暴露而非静默丢规则）

全程离线（KB 是本地 markdown，无外部依赖）。
"""

import pytest

from app.llm.kb_loader import kb, _split_sections
from app.llm.reasoner import _build_campaign_system_prompt, _build_new_campaign_prompt


# ── 切块器 ───────────────────────────────────────────────

def test_split_sections_parses_numbered_headings():
    text = "导语\n\n## 1. 甲\n内容A\n\n## 2. 乙\n内容B\n\n## 7A. 丙\n内容C\n"
    secs = _split_sections(text)
    assert set(secs) == {"1", "2", "7A"}
    assert secs["1"].startswith("## 1. 甲")
    assert "内容B" in secs["2"]


def test_split_sections_parses_numbered_subheadings():
    text = (
        "## 3. 动作矩阵\n总则\n\n"
        "### 3.1 排名\n排名动作\n\n"
        "### 3.2 扩词\n扩词动作\n\n"
        "## 4. 冲突\n冲突规则\n"
    )
    secs = _split_sections(text)
    assert "## 3. 动作矩阵" in secs["3"]
    assert "排名动作" in secs["3.1"]
    assert "扩词动作" not in secs["3.1"]
    assert "冲突规则" not in secs["3.2"]


def test_split_sections_empty_for_unnumbered():
    # 无 "## N." 编号标题（如 KB08）→ 空 dict，build 退回整文件
    assert _split_sections("## 市场竞争态势字段\n表格\n## 竞品规则\n内容") == {}


def test_real_kb_sections_indexed():
    # KB17 应切出含 7A 的多节；KB08 无编号 → 空
    assert "7A" in kb._sections.get("17", {})
    assert kb._sections.get("08", {}) == {}


# ── 体量压缩（不回退整文件 48K）────────────────────────────

@pytest.mark.parametrize("preset,ceiling", [
    ("campaign_adjustment_exact", 32000),   # 实测 ~24K，整文件 48K
    ("campaign_adjustment_broad", 32000),   # 实测 ~22K
    ("new_campaign", 11000),                # 实测 ~7.8K，整文件 ~14K
])
def test_preset_compressed(preset, ceiling):
    assert 0 < len(kb.build(preset)) < ceiling


# ── 关键规则零丢失（精准 + 广泛都必须保留）──────────────────

@pytest.mark.parametrize("rule", [
    "BUDGET_CANT_SPEND",      # KB17 §1 花不出去诊断
    "预算调整决策矩阵",        # KB19 §6（不能花完→维持，预算 bug 关键）
    "活动预算不能花完",        # KB19 §6 分支
    "有效容忍上限",            # KB17 §2 / KB15 §4
    "花费级别",                # KB19 §2 利用率<50%=低花费
])
def test_key_rules_retained_both_streams(rule):
    assert rule in kb.build("campaign_adjustment_exact")
    assert rule in kb.build("campaign_adjustment_broad")


# ── 噪声剔除 ─────────────────────────────────────────────

def test_exact_drops_noise():
    ex = kb.build("campaign_adjustment_exact")
    assert "组合预算回算与二次分配" not in ex      # KB17 §8（属 budget_reallocation agent）
    assert "13 步 AI 广告调整总流程" not in ex     # KB18 §2（开发编排指南）
    assert "### 精准广告调整示例" not in ex        # KB22 §5（90行YAML，与 prompt 输出冲突）


def test_stream_split_placement_vs_negative():
    ex = kb.build("campaign_adjustment_exact")
    br = kb.build("campaign_adjustment_broad")
    # 精准要广告位、不要广泛的精准节噪声重复；广泛禁 placement、要否词
    assert "主投广告位选择矩阵" in ex and "主投广告位选择矩阵" not in br
    assert "否词触发规则" in br


def test_campaign_exact_uses_only_requested_direction_matrix():
    exact = kb.build_campaign_adjustment("exact", ["优化ACOS"])
    assert "### 3.3 广告方向 = `优化ACOS`" in exact
    assert "### 3.1 广告方向 = `推进自然位`" not in exact
    assert "### 3.2 广告方向 = `新增扩词`" not in exact
    assert "### 3.4 广告方向 = `平衡维持`" not in exact


def test_campaign_adjustment_drops_elimination_follow_up_noise():
    exact = kb.build_campaign_adjustment("exact", ["优化ACOS"])
    broad = kb.build_campaign_adjustment("broad", ["优化ACOS"])
    for content in (exact, broad):
        assert "淘汰释放预算回算" not in content
        assert "淘汰后重新启用判断" not in content


def test_campaign_prompt_is_kb_first_and_direction_scoped():
    prompt = _build_campaign_system_prompt("exact", ["优化ACOS"])
    assert "业务知识优先级（必须遵循）" in prompt
    assert "不得引用未注入的 KB 章节或自行补充规则" not in prompt
    assert "不得仅因当前 Bid 或 Budget 处于低值就跳过诊断直接淘汰" not in prompt
    assert "当前 Bid ≤ $0.20 或 当前日预算 ≤ $1.00" not in prompt
    assert "KB18 §1-4/§6" not in prompt
    assert "KB22 §1" not in prompt
    assert "### 3.3 广告方向 = `优化ACOS`" in prompt
    assert "### 3.1 广告方向 = `推进自然位`" not in prompt


def test_new_campaign_prompt_uses_only_its_decision_scope():
    prompt = _build_new_campaign_prompt()
    assert "本调用只做逐词相关性与新建判断" not in prompt
    assert "竞品Deal压价" not in prompt
    assert "扩词场景识别框架" not in prompt
    assert "场景化扩词配额" not in prompt


def test_new_campaign_prompt_explains_rank_history_evidence_states():
    prompt = _build_new_campaign_prompt()

    assert "search_rank" in prompt
    assert "rank_trend" in prompt
    assert "rank_tier" in prompt
    assert "sponsored_rank" in prompt
    assert "not_eligible / query_failed" in prompt
    assert "不等同于“无自然位”" in prompt


def test_new_campaign_drops_code_numerics():
    nc = kb.build("new_campaign")
    # KB16 §2/§3/§5：预算/bid/输出schema 是代码用的，prompt 禁 LLM 输出 → 噪声
    assert "初始预算推荐" not in nc
    assert "新增活动输出要求" not in nc
    assert "## 7. 特殊场景" not in nc              # KB02 §7（与建词无关）
    # 但触发场景 + 阻断 + keyword_class 判定依据必须在
    assert "触发场景" in nc and "阻断条件" in nc
    assert "Long Tail" in nc                       # KB06 keyword_class 依据
    assert "扩词场景识别框架" not in nc              # KB28 §0 非逐词相关性判断
    assert "场景化扩词配额" not in nc                # KB28 §3 非逐词相关性判断
    assert "竞品Deal压价" not in nc                 # KB08 缺少竞品事实字段，不注入


# ── 迭代护栏 + 接口契约 ──────────────────────────────────

def test_missing_section_warns(caplog):
    # KB 改版重编号/preset 写错节号 → 必须告警暴露，不静默丢规则
    kb.PRESETS["_slice_probe"] = ["17:999"]
    try:
        with caplog.at_level("WARNING"):
            kb.build("_slice_probe")
        assert any("缺节" in r.getMessage() and "999" in r.getMessage()
                   for r in caplog.records)
    finally:
        del kb.PRESETS["_slice_probe"]


def test_whole_file_spec_backward_compatible():
    # 无冒号 spec = 整文件（chat preset 用整文件），行为不变
    assert len(kb.build("chat")) > 0


def test_unknown_preset_raises():
    with pytest.raises(KeyError):
        kb.build("不存在的preset")
