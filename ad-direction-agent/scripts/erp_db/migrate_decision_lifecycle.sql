-- ============================================================
-- 决策批次生命周期迁移 (2026-06-12)
-- 数据库: erp_agentadvert@192.168.2.51
-- 执行前: 备份 t_advert_agent_decision
-- ============================================================

-- 1. 新增生命周期列
ALTER TABLE t_advert_agent_decision
    ADD COLUMN decision_status VARCHAR(16) NOT NULL DEFAULT 'COMPLETED'
        COMMENT 'DRAFT/COMPLETED/ARCHIVED',
    ADD COLUMN is_latest       TINYINT(1)  NOT NULL DEFAULT 0
        COMMENT '该 ASIN 最新已完成批次',
    ADD COLUMN analysis_mode   VARCHAR(16) NOT NULL DEFAULT 'REALTIME'
        COMMENT 'REALTIME/SCHEDULED';

-- 2. 索引
ALTER TABLE t_advert_agent_decision
    ADD INDEX idx_decision_asin_latest (parent_asin, is_latest),
    ADD INDEX idx_decision_asin_status (parent_asin, decision_status);

-- 3. 数据回填: 存量行均为 COMPLETED
--    is_latest 限定 COMPLETED 批次中每 ASIN 选 create_time 最新者
--    (用窗口函数，避免非 LATERAL 子查询引用外层 d.parent_asin 报 1054)
UPDATE t_advert_agent_decision d SET is_latest = 0;

UPDATE t_advert_agent_decision d
JOIN (
    SELECT id FROM (
        SELECT id,
            ROW_NUMBER() OVER (PARTITION BY parent_asin ORDER BY create_time DESC, id DESC) AS rn
        FROM t_advert_agent_decision
        WHERE decision_status = 'COMPLETED'
    ) ranked WHERE rn = 1
) pick ON pick.id = d.id
SET d.is_latest = 1;
