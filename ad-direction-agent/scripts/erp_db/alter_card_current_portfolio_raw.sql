-- 2026-08-10: 将已存在的 current_portfolio 扩展为可保存未知 MCP 组合原名。
-- 该列不是 target；campaign_group_type 才是确定性目标 ERP 码。
ALTER TABLE t_advert_agent_modify_suggest_card
    MODIFY COLUMN current_portfolio VARCHAR(512) DEFAULT NULL
    COMMENT '真实 MCP 当前组合（标准组为 ERP 码，未知名称原样保留）';
