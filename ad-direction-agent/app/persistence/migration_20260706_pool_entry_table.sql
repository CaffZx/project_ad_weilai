-- migration_20260706: 淘汰复评数据源重建（KB 21 §7）
--
-- 背景：原复评入口 get_elimination_entry_dates 依赖 card+pending 表
--   suggest_category='ELIMINATE' AND confirm_status='CONFIRMED'，
--   一步直跑(submit_execution_direct)不写 confirm_status → 恒空 → 复评门永不进。
--
-- 方案：自建 t_advert_agent_pool_entry，由分析后双向同步(sync_pool_entries)
--   discovery 入池补录 + 手动复评/恢复离池检测 + 执行钩子(后续)维护。
--   复评链 get_active_entries 读本品。
--
-- 目标库：ERP 库（与 card/pending 主表同库）
-- 执行方式：上线前在 ERP 库手工执行一次；idempotent（IF NOT EXISTS）。

CREATE TABLE IF NOT EXISTS t_advert_agent_pool_entry (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    shop_id       INT          NULL,             -- discovery 时未知
    shop_account  VARCHAR(64)  NULL,             -- discovery 时可能无 DB 上下文
    parent_asin   VARCHAR(50)  NOT NULL,
    parent_sku    VARCHAR(64)  NULL,             -- 父 SKU（discovery 时从 campaign_data 取，执行钩子从 decision 取）
    child_asin    VARCHAR(64)  NULL,             -- 灰卡/手动入池可能无子 ASIN
    campaign_id   VARCHAR(64)  NOT NULL,
    campaign_key  VARCHAR(600) NULL,             -- discovery 时可能为空
    campaign_name VARCHAR(512) NOT NULL,
    keyword_text  VARCHAR(255) NULL,
    decision_id   VARCHAR(64)  NULL,             -- discovery 时为 NULL
    source        VARCHAR(16)  NOT NULL DEFAULT 'discovery',
    entry_date    DATETIME(6)  NOT NULL,
    exit_date     DATETIME(6)  NULL,
    eliminate_spend_7d DECIMAL(12,2) NULL,
    create_time   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    update_time   DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_asin_cid (parent_asin, campaign_id),
    INDEX idx_parent_exit (parent_asin, exit_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
