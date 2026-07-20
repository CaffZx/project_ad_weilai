-- migrate_portfolio_spend_windows.sql
-- 新增 t_advert_agent_modify_suggest_summary 组合看板花费字段（1/3/7 天窗口）。
-- 字段语义：最近 N 个完整自然日的窗口总花费（按站点当地时间，排除当天）。
-- 仅用于前端看板，不参与 LLM 预算回算。
-- 前置：无外部依赖，独立执行即可。
-- 回滚：ALTER TABLE ... DROP COLUMN 各列。

ALTER TABLE t_advert_agent_modify_suggest_summary
    ADD COLUMN main_push_spend_1d DECIMAL(10,2) DEFAULT NULL COMMENT '精准主力组最近1日总实际花费',
    ADD COLUMN main_push_spend_3d DECIMAL(10,2) DEFAULT NULL COMMENT '精准主力组最近3日总实际花费',
    ADD COLUMN main_push_spend_7d DECIMAL(10,2) DEFAULT NULL COMMENT '精准主力组最近7日总实际花费',
    ADD COLUMN broad_auto_spend_1d DECIMAL(10,2) DEFAULT NULL COMMENT '自动广泛组最近1日总实际花费',
    ADD COLUMN broad_auto_spend_3d DECIMAL(10,2) DEFAULT NULL COMMENT '自动广泛组最近3日总实际花费',
    ADD COLUMN broad_auto_spend_7d DECIMAL(10,2) DEFAULT NULL COMMENT '自动广泛组最近7日总实际花费',
    ADD COLUMN test_new_spend_1d DECIMAL(10,2) DEFAULT NULL COMMENT '精准测试组最近1日总实际花费',
    ADD COLUMN test_new_spend_3d DECIMAL(10,2) DEFAULT NULL COMMENT '精准测试组最近3日总实际花费',
    ADD COLUMN test_new_spend_7d DECIMAL(10,2) DEFAULT NULL COMMENT '精准测试组最近7日总实际花费',
    ADD COLUMN eliminate_bubble_spend_1d DECIMAL(10,2) DEFAULT NULL COMMENT '低价捡漏组最近1日总实际花费',
    ADD COLUMN eliminate_bubble_spend_3d DECIMAL(10,2) DEFAULT NULL COMMENT '低价捡漏组最近3日总实际花费',
    ADD COLUMN eliminate_bubble_spend_7d DECIMAL(10,2) DEFAULT NULL COMMENT '低价捡漏组最近7日总实际花费',
    ADD COLUMN main_push_acos_1d DECIMAL(6,4) DEFAULT NULL COMMENT '精准主力组ACOS(1d)',
    ADD COLUMN main_push_acos_3d DECIMAL(6,4) DEFAULT NULL COMMENT '精准主力组ACOS(3d)',
    ADD COLUMN main_push_acos_7d DECIMAL(6,4) DEFAULT NULL COMMENT '精准主力组ACOS(7d)',
    ADD COLUMN broad_auto_acos_1d DECIMAL(6,4) DEFAULT NULL COMMENT '自动广泛组ACOS(1d)',
    ADD COLUMN broad_auto_acos_3d DECIMAL(6,4) DEFAULT NULL COMMENT '自动广泛组ACOS(3d)',
    ADD COLUMN broad_auto_acos_7d DECIMAL(6,4) DEFAULT NULL COMMENT '自动广泛组ACOS(7d)',
    ADD COLUMN test_new_acos_1d DECIMAL(6,4) DEFAULT NULL COMMENT '精准测试组ACOS(1d)',
    ADD COLUMN test_new_acos_3d DECIMAL(6,4) DEFAULT NULL COMMENT '精准测试组ACOS(3d)',
    ADD COLUMN test_new_acos_7d DECIMAL(6,4) DEFAULT NULL COMMENT '精准测试组ACOS(7d)',
    ADD COLUMN eliminate_bubble_acos_1d DECIMAL(6,4) DEFAULT NULL COMMENT '低价捡漏组ACOS(1d)',
    ADD COLUMN eliminate_bubble_acos_3d DECIMAL(6,4) DEFAULT NULL COMMENT '低价捡漏组ACOS(3d)',
    ADD COLUMN eliminate_bubble_acos_7d DECIMAL(6,4) DEFAULT NULL COMMENT '低价捡漏组ACOS(7d)';
