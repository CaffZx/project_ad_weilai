-- migration: rename keyword_types → target_keyword_strategy
-- run against ad_agent_state database
-- 2026-05-30

ALTER TABLE tactics_config CHANGE COLUMN keyword_types target_keyword_strategy JSON;
