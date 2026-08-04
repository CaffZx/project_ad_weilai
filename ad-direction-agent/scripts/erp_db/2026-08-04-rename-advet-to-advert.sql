-- 2026-08-04: 表名拼写订正 t_advet_agent_config → t_advert_agent_config
-- 背景：该表名是历史缩写（advet），全库其它 t_advert_agent_* 表均用全拼 advert，
--      这是唯一异类。本期"一键保存 + agent_config 回读"方案新增对这张表的读路径，
--      顺势把表名理顺，消除技术债。
-- 影响：MySQL RENAME TABLE 是元数据级操作，不改数据、不重建表、毫秒级完成；
--       生产数据数百行原样保留，只是表名换。
-- 依赖核实（已确认）：该表无外键引用、无视图依赖、无触发器引用。
-- 代码侧：app/persistence/erp_writer/repository.py 与 app/data/decision_config_reader.py
--       的 SQL 字符串已同步改为 t_advert_agent_config，与本 RENAME 同一窗口部署。
-- 部署顺序：先执行本 RENAME，再部署改名后的代码（或同一维护窗口内几分钟完成）。
--       窗口期内若新代码先于 RENAME 上线，访问旧表名会报 Table 不存在，但被
--       mirror_agent_config 的 logger.exception 吞掉，主保存链路不受影响，仅日志告警。
-- 回滚：RENAME TABLE t_advert_agent_config TO t_advet_agent_config;

RENAME TABLE t_advet_agent_config TO t_advert_agent_config;