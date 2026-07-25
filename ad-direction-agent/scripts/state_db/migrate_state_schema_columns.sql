-- State 库历史升级 DDL。
-- 应用启动不再执行列检查或 ALTER TABLE；请按目标库版本人工执行对应语句。
-- 所有字段均与 app/persistence/schema.sql 保持一致。

ALTER TABLE strategy_config
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL,
    ADD COLUMN operating_mode VARCHAR(32) NULL;

ALTER TABLE tactics_config
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE acos_override
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE budget_override
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE adjustment_history
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE p3_recommendation
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE keyword_analysis
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE target_scores
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE workflow_meta
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE feedback
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL;

ALTER TABLE analysis_session
    ADD COLUMN shop_id BIGINT NULL,
    ADD COLUMN parent_seller_sku VARCHAR(128) NULL,
    ADD COLUMN execution_started_at DATETIME(6) NULL;
