-- 分析事件协作取消：为已有 analysis_session 表增加 cancel_requested_at 列
-- 上线顺序：先执行本 DDL，再部署读取该列的代码
ALTER TABLE analysis_session
    ADD COLUMN cancel_requested_at DATETIME(6) NULL
    AFTER execution_started_at;
