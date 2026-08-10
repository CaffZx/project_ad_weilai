-- 2026-08-10: current_portfolio 保存真实 MCP 当前组合；标准组归一化为 ERP 码，未知名称原样保留。
-- campaign_group_type 始终保存确定性 target ERP 码；两列不再互相回落。
ALTER TABLE t_advert_agent_modify_suggest_card
    ADD COLUMN current_portfolio VARCHAR(512) DEFAULT NULL COMMENT '真实 MCP 当前组合（标准组为 ERP 码，未知名称原样保留）' AFTER campaign_group_type;
