# StarRocks/Doris 索引建议（供 DBA，应用侧不执行）

当前应用已通过 SQL 改写（两步 flow 查、子 ASIN `IN` 列表）降低扫描量。以下索引可进一步缩短查询时间。

## `dwd_amazon_listing_flow_keyword_us`

```sql
-- 按店铺 + 父 ASIN 过滤流量词（Step A）
CREATE INDEX idx_flow_shop_parent ON dwd_amazon_listing_flow_keyword_us (shop_id, parent_asin, del_status);
```

## `dwd_amazon_precise_keyword_library`

```sql
-- Step B 批量 IN 查词库
CREATE INDEX idx_precise_site_kw ON dwd_amazon_precise_keyword_library (site_code, keyword);
```

## `dwd_amazon_ad_keyword_report`

```sql
CREATE INDEX idx_kw_report_shop_time ON dwd_amazon_ad_keyword_report (shop_id, LOCAL_REPORT_TIME);
```

## `dwd_amazon_asin_keyword_library`

```sql
CREATE INDEX idx_kw_lib_asin_time ON dwd_amazon_asin_keyword_library (asin, craw_time);
```

## `dwd_amazon_ad_product_report_update`

```sql
CREATE INDEX idx_ad_prod_shop_time ON dwd_amazon_ad_product_report_update (shop_id, LOCAL_REPORT_TIME, asin);
```

## 运维项（非索引）

- 日志中的 **1064 HDFS cache directory** 需在 StarRocks BE 节点处理，应用层无法修复。
