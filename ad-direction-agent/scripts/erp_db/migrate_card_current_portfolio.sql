-- 2026-08-07: 挪组建议前端显示 — card 表加当前组合列
-- 现有 campaign_group_type 存储目标组（有挪组时）或当前组（无挪组时），语义为"有效组"
-- 新增 current_portfolio 存储当前组 ERP 码，供前端渲染"当前组 → 目标组"挪组行
ALTER TABLE t_advert_agent_modify_suggest_card
    ADD COLUMN current_portfolio VARCHAR(32) DEFAULT NULL COMMENT '当前组ERP码' AFTER campaign_group_type;
