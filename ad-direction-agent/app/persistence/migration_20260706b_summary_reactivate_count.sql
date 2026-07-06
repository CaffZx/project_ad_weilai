-- migration_20260706b: t_advert_agent_modify_suggest_summary 增 reactivate_count 列
--
-- 背景：KB21 §7 复评卡 action=reactivate_* 落 suggest_category='REACTIVATE'（mappers 已加映射）。
--   summary 表原只有 eliminate_count/adjust_count/keep_count/create_count 四列,
--   复评被硬塞进 adjust_count,语义不清。本迁移加独立列,与历史 count 列同口径。
--
-- 历史数据：无——复评功能此前因入池数据源断,从未真出过卡。新列对老批次默认 0,无回填需求。
-- 兼容：NOT NULL DEFAULT 0,与既有 count 列一致；新行 INSERT 须显式赋值,老行自动填 0,SQL 不报错。
-- 目标库：ERP 库（与 summary 主表同库）
-- 执行方式：上线前在 ERP 库手工执行一次；idempotent（IF NOT EXISTS via information_schema 检查）。

ALTER TABLE t_advert_agent_modify_suggest_summary
    ADD COLUMN reactivate_count INT NOT NULL DEFAULT 0
    COMMENT 'KB21 §7 复评活动数（reactivate_budget_only / reactivate_with_calibrated_bid）'
    AFTER keep_count;