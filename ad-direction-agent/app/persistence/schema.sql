-- 长期状态库 schema（独立 MySQL，与 Doris 业务库分离）
CREATE DATABASE IF NOT EXISTS ad_agent_state DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE ad_agent_state;

CREATE TABLE IF NOT EXISTS strategy_config (
    asin VARCHAR(20) PRIMARY KEY,
    product_level VARCHAR(32),
    product_stage VARCHAR(32),
    season_stage VARCHAR(32),
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS tactics_config (
    asin VARCHAR(20) PRIMARY KEY,
    ad_purposes JSON,
    target_keyword_strategy JSON,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS acos_override (
    asin VARCHAR(20) PRIMARY KEY,
    value INT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS budget_override (
    asin VARCHAR(20) PRIMARY KEY,
    value DOUBLE NOT NULL,
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS adjustment_history (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    asin VARCHAR(20) NOT NULL,
    record_date DATE NOT NULL,
    target_acos INT,
    daily_budget DOUBLE,
    operated_at DATETIME(6) NOT NULL,
    UNIQUE KEY uq_asin_date (asin, record_date),
    INDEX idx_asin (asin)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS p3_recommendation (
    asin VARCHAR(20) PRIMARY KEY,
    payload JSON NOT NULL,
    created_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS keyword_analysis (
    asin VARCHAR(20) NOT NULL,
    days INT NOT NULL,
    payload JSON NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (asin, days)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS target_scores (
    asin VARCHAR(20) NOT NULL,
    days INT NOT NULL,
    payload JSON NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (asin, days)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS workflow_meta (
    asin VARCHAR(20) PRIMARY KEY,
    current_layer VARCHAR(32) NOT NULL DEFAULT 'strategy',
    layers_completed JSON,
    execution_selection JSON,
    updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS feedback (
    id VARCHAR(64) PRIMARY KEY,
    asin VARCHAR(20) NOT NULL,
    payload JSON NOT NULL,
    submitted_at DATETIME(6) NOT NULL,
    INDEX idx_feedback_asin (asin)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS analysis_session (
    asin VARCHAR(20) PRIMARY KEY,
    run_id VARCHAR(64) NOT NULL,
    started_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
