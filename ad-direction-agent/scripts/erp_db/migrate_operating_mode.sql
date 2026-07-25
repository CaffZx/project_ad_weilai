-- 经营模式：只增加可空字段；不回填历史记录，不设置默认值。
-- 运行目标：erp_agentadvert。此文件仅作为本地版本化迁移，不会由应用自动执行。

ALTER TABLE t_advert_agent_decision_config
    ADD COLUMN operating_mode VARCHAR(32) NULL;

ALTER TABLE t_advert_agent_decision
    ADD COLUMN operating_mode VARCHAR(32) NULL;
