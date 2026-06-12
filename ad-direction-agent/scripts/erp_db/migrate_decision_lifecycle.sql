-- ============================================================
-- 决策批次 is_latest 修复 (2026-06-12)
-- 数据库: erp_agentadvert@192.168.2.51
--
-- 背景: 库改造已加 is_latest / analysis_mode / product_name 列。但:
--   (1) is_latest 默认 1 且回填未跑 → 每 ASIN 多行 is_latest=1，get_latest_completed 失效；
--   (2) 不引入 decision_status 列 —— decision_id 在 write_full 落库时生成，
--       所有 decision 行即"已完成批次"；"进行中"态用 run_id 落 state 库
--       (ad_agent_state.analysis_session)，不污染 ERP 库。
-- 本脚本只修 is_latest（列默认值 + 存量回填）。
-- 连接注意: caching_sha2_password，pymysql 可经公钥握手非 TLS 连通（实测 OK）。
-- ============================================================

-- 1. 列默认值改 0：新行不再默认"最新"，由 finalize_batch 写库后显式置位（fail-closed）
ALTER TABLE t_advert_agent_decision
    MODIFY is_latest TINYINT(1) NOT NULL DEFAULT 0 COMMENT '该ASIN最新批次(我方 finalize_batch 维护)';

-- 2. 存量回填：全部清 0，再每 ASIN 取 create_time 最新者置 1（窗口函数，避非 LATERAL 报 1054）
UPDATE t_advert_agent_decision SET is_latest = 0;

UPDATE t_advert_agent_decision d
JOIN (
    SELECT id FROM (
        SELECT id,
            ROW_NUMBER() OVER (PARTITION BY parent_asin ORDER BY create_time DESC, id DESC) AS rn
        FROM t_advert_agent_decision
    ) ranked WHERE rn = 1
) pick ON pick.id = d.id
SET d.is_latest = 1;
