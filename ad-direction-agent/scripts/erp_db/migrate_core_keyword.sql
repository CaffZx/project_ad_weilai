-- 核心词判定系统 DDL
-- 迁移日期：2026-07-14
-- 目标库：erp_agentadvert_chen（ERP 库）

CREATE TABLE IF NOT EXISTS t_advert_agent_core_keyword_task (
    id                  VARCHAR(32)  NOT NULL,
    parent_asin         VARCHAR(20)  NOT NULL,
    parent_seller_sku   VARCHAR(128) NOT NULL,
    shop_id             BIGINT       NOT NULL,
    site_code           VARCHAR(10)  NULL,
    status              VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
    total_keyword_count INT          NOT NULL DEFAULT 0,
    core_keyword_count  INT          NOT NULL DEFAULT 0,
    error_message       TEXT         NULL,
    triggered_by        VARCHAR(64)  NULL,
    started_at          DATETIME(6)  NOT NULL,
    finished_at         DATETIME(6)  NULL,
    PRIMARY KEY (id),
    KEY idx_product (parent_asin, parent_seller_sku, shop_id),
    KEY idx_status (status),
    KEY idx_latest (parent_asin, parent_seller_sku, shop_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS t_advert_agent_core_keyword_label (
    id              BIGINT AUTO_INCREMENT,
    task_id         VARCHAR(32)  NOT NULL,
    parent_asin     VARCHAR(20)  NOT NULL,
    parent_seller_sku VARCHAR(128) NOT NULL,
    shop_id         BIGINT       NOT NULL,
    keyword_text    VARCHAR(512) NOT NULL,
    semantic_conflict VARCHAR(10) NOT NULL DEFAULT 'pass',
    conflict_reason TEXT         NULL,
    semantic_core   TINYINT(1)   NOT NULL DEFAULT 0,
    semantic_evidence JSON       NULL,
    data_core       TINYINT(1)   NOT NULL DEFAULT 0,
    data_evidence   JSON         NULL,
    is_core         TINYINT(1)   NOT NULL DEFAULT 0,
    source_refs     JSON         NULL,
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_task_keyword (task_id, keyword_text),
    KEY idx_product_keyword (parent_asin, parent_seller_sku, shop_id, keyword_text),
    CONSTRAINT fk_core_keyword_task
        FOREIGN KEY (task_id) REFERENCES t_advert_agent_core_keyword_task(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
