-- 核心词人工状态管理表
-- 迁移日期：2026-07-16
-- 目标库：erp_agentadvert_chen（ERP 库）

CREATE TABLE IF NOT EXISTS t_advert_agent_core_keyword_state (
    id                    BIGINT       NOT NULL AUTO_INCREMENT,
    parent_asin           VARCHAR(20)  NOT NULL,
    parent_seller_sku     VARCHAR(128) NOT NULL,
    shop_id               BIGINT       NOT NULL,
    keyword_text          VARCHAR(512) NOT NULL,
    keyword_norm          VARCHAR(512) NOT NULL,
    state                 ENUM('LOCKED', 'ENABLED', 'DISABLED', 'VETOED')
                          NOT NULL DEFAULT 'ENABLED',
    base_task_id          VARCHAR(32)  NULL,
    base_task_finished_at DATETIME(6)  NULL,
    operator              VARCHAR(64)  NULL,
    created_at            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at            DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                           ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_product_keyword (
        parent_asin, parent_seller_sku, shop_id, keyword_norm
    ),
    KEY idx_product_state (parent_asin, parent_seller_sku, shop_id, state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
