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


# ── 切块器 ───────────────────────────────────────────────

def test_split_sections_parses_numbered_headings():
    text = "导语\n\n## 1. 甲\n内容A\n\n## 2. 乙\n内容B\n\n## 7A. 丙\n内容C\n"
    secs = _split_sections(text)
    assert set(secs) == {"1", "2", "7A"}
    assert secs["1"].startswith("## 1. 甲")
    assert "内容B" in secs["2"]


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


def test_new_campaign_drops_code_numerics():
    nc = kb.build("new_campaign")
    # KB16 §2/§3/§5：预算/bid/输出schema 是代码用的，prompt 禁 LLM 输出 → 噪声
    assert "初始预算推荐" not in nc
    assert "新增活动输出要求" not in nc
    assert "## 7. 特殊场景" not in nc              # KB02 §7（与建词无关）
    # 但触发场景 + 阻断 + keyword_class 判定依据必须在
    assert "触发场景" in nc and "阻断条件" in nc
    assert "Long Tail" in nc                       # KB06 keyword_class 依据


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
