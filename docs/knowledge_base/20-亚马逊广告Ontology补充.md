# 亚马逊广告 Ontology 补充（草案）

> **状态：`reference_only`**（`00号§4.2`）。本文是 Ontology 的**设计说明文档**，不注入 prompt 切片。
> 运行时口径由 `ontology/runtime_contract.yaml`（注入 Ontology Card）与 `OntologyValidator`
> （执行 `ONT-*` / `GROUP-*` 校验）承担。
>
> v3.3.0 本文连 `kb_loader._FILE_PATHS` 里都没有 fid，加载器不认识它——既不是切片也没标注，属规则悬空。

> 本文件为新增补充文件，不修改原有 SOP 规则。  
> 目的：先把 Amazon 官方业务对象、广告对象层级、投放类型、动作合法性、报表证据关系补齐，后续可再拆成独立 ontology 层。

## 0. 与经营闭环 Ontology 的连接（C01）

- `OperatingUnitRef`：`shop_id + site_code + parent_asin`，是广告决策与执行的业务主语。
- `ChildScope`：只描述少数子 ASIN 例外，不替代父级经营单元。
- `AdvertisingDecision`、`ActionOrder`、`ActionReceipt` 必须引用同一个
  `operating_unit_id`。
- `parent_seller_sku` 是 ERP/MCP 映射属性，不参与业务唯一键。
- 旧 ASIN 级状态标记为 `IDENTITY_UNRESOLVED`，完成店铺和站点绑定前不可执行。

---

## 1. 参考来源

| 来源 | 可借鉴内容 |
| --- | --- |
| Amazon Sponsored Products Best Practices | Sponsored Products、Campaign、预算、Bid、Placement、Search Term Report、Targeting Report、Placement Report |
| Amazon Sponsored Products Targeting Guide | 自动投放、手动关键词投放、商品投放、否定投放、匹配方式 |
| Amazon SP-API Product Type Definitions | 商品类型、属性、必填项、条件 schema |
| Amazon Product Advertising API Browse Nodes | Amazon Browse Node 类目层级 |

---

## 2. 商品与类目 Ontology

### 2.1 商品目录对象

| 类 | 中文名 | 说明 | 关键关系 |
| --- | --- | --- | --- |
| `Marketplace` | 亚马逊站点 | 如 US / CA / UK，不同站点的类目、CPC、季节性、Listing 要求可能不同 | `hasBrowseNode` / `hasProductTypeDefinition` |
| `BrowseNode` | 浏览节点/类目节点 | Amazon 用层级节点组织商品集合；节点不是单个商品 | `parentOf` / `childOf` / `containsProductType` |
| `ProductTypeDefinition` | 商品类型定义 | SP-API 可返回指定 marketplace 与 product type 的属性、要求和条件 schema | `definesRequiredAttribute` / `appliesToMarketplace` |
| `ParentASIN` | 父 ASIN | 变体集合，适合作为广告策略和库存风险分析主体 | `hasChildASIN` / `hasChildSKU` |
| `ChildASIN` | 子 ASIN | 具体变体商品 | `belongsToParentASIN` / `hasListing` |
| `SKU` | 卖家 SKU | 库存、履约和 ERP 管理对象 | `mapsToChildASIN` / `hasInventoryStatus` |
| `Listing` | 商品详情页 | 广告点击后的承接页面 | `hasRating` / `hasReturnRisk` / `hasConversionSignal` |

### 2.2 关键约束

| 规则 | 内容 |
| --- | --- |
| CAT-001 | `BrowseNode` 是类目集合节点，不等于 ASIN，也不等于 Product Type。 |
| CAT-002 | `ProductTypeDefinition` 用于 Listing 属性完整性和类目要求判断，不直接决定广告 Bid。 |
| CAT-003 | `ParentASIN` 适合做策略标签、预算、库存风险和父体维度汇总。 |
| CAT-004 | `ChildASIN` / `SKU` 适合做变体库存、核心 SKU 短缺、子体承接能力判断。 |

---

## 3. Sponsored Products 广告对象 Ontology

### 3.1 广告对象层级

| 类 | 中文名 | 说明 | 关键关系 |
| --- | --- | --- | --- |
| `AdsProfile` | 广告账号配置 | Amazon Ads API 操作范围 | `ownsCampaign` / `ownsPortfolio` |
| `Portfolio` | 广告组合 | 用于按品牌、类目、季节、项目组织 Campaign | `containsCampaign` |
| `Campaign` | 广告活动 | 预算、起止时间、竞价策略、广告位加价的主要容器 | `containsAdGroup` / `hasDailyBudget` |
| `AdGroup` | 广告组 | 承载商品广告和投放目标 | `containsProductAd` / `containsTarget` |
| `ProductAd` | 商品广告 | 被推广的 ASIN/SKU | `promotesChildASIN` / `belongsToAdGroup` |
| `TargetingClause` | 投放目标 | Keyword / Product / Auto Target 的统一父类 | `belongsToAdGroup` / `hasBid` |
| `KeywordTarget` | 关键词目标 | 手动关键词投放对象 | `hasMatchType` / `hasBid` |
| `ProductTarget` | 商品目标 | ASIN 或类目投放对象 | `targetsASIN` / `targetsCategory` |
| `AutoTarget` | 自动投放目标 | close_match / loose_match / substitutes / complements | `hasAutoTargetType` |
| `NegativeTarget` | 否定目标 | 否定关键词、否定商品、否定品牌 | `excludesTrafficFrom` |
| `SearchTerm` | 搜索词 | 用户实际搜索或触发广告的查询词，是报表证据 | `canPromoteToKeywordTarget` / `canCreateNegativeTarget` |
| `Placement` | 广告位 | top_of_search / rest_of_search / product_pages | `hasPlacementAdjustment` |

### 3.1A 广告组合与预算控制对象 Ontology

| 类 | 中文名 | 说明 | 关键关系 |
| --- | --- | --- | --- |
| `AdBudgetGroup` | 广告预算组 | 父 ASIN 下的预算控制对象，对应精准主力组 / 精准测试组 / 广泛自动组 / 低价捡漏组 | `belongsToParentASIN` / `containsCampaign` |

补充关系：

- `ParentASIN hasBudgetGroup AdBudgetGroup`
- `AdBudgetGroup containsCampaign`
- `Campaign belongsToBudgetGroup`
- `Portfolio containsCampaign`
- `Portfolio ≠ AdBudgetGroup`

### 3.2 对象边界规则

| 规则 | 内容 |
| --- | --- |
| OBJ-001 | `SearchTerm` 不是 `KeywordTarget`，不能直接调 Bid。 |
| OBJ-002 | 搜索词只能用于：新增关键词、创建否定词、作为证据。 |
| OBJ-003 | `Placement` 调整不能绑定到单个 Search Term。 |
| OBJ-004 | `KeywordTarget`、`ProductTarget`、`AutoTarget` 都是投放目标，但可用动作不同。 |
| OBJ-005 | 广告位调整必须依赖 Placement 数据；缺少 Placement 数据时仅阻断广告位动作，不阻断 Bid / Budget / 否词动作。 |

---

## 4. Campaign Type Ontology

| campaign_type | 来源 | 允许目标 | 允许动作 | 禁止动作 | 主要用途 |
| --- | --- | --- | --- | --- | --- |
| `exact_keyword` | 手动关键词投放 | `KeywordTarget(match_type=EXACT)` | Bid / Budget / Placement / 关闭 | 否词、盲目扩泛词 | 承接已验证词、推自然位、稳定转化 |
| `phrase_keyword` | 手动关键词投放 | `KeywordTarget(match_type=PHRASE)` | Bid / Budget / Search Term 否词 / 提词 | Placement 调整 | 扩展中等相关搜索词 |
| `broad_keyword` | 手动关键词投放 | `KeywordTarget(match_type=BROAD)` | Bid / Budget / Search Term 否词 / 提词 | Placement 调整 | 打开词池、获取搜索词样本 |
| `auto_discovery` | 自动投放 | `AutoTarget` | Target Bid / Budget / Search Term 否词与提取 | Placement 调整；缺少四投放组契约时执行 Target Bid | 发现关键词、商品和类目机会 |
| `product_targeting` | 手动商品/类目投放 | `ProductTarget` | Bid / Budget / Placement | 关键词匹配方式调整 | 竞品截流、类目拓展、防守自家页面 |

---

## 5. 自动投放 Ontology

| auto_target_type | 中文含义 | 典型用途 |
| --- | --- | --- |
| `close_match` | 紧密匹配 | 发现与商品高度相关的搜索词 |
| `loose_match` | 宽泛匹配 | 扩展更宽的搜索词样本 |
| `substitutes` | 替代品 | 出现在相似商品详情页，适合竞品截流观察 |
| `complements` | 互补品 | 出现在互补商品详情页，适合场景化拓展 |

---

## 6. 广告目的与广告方向 Ontology

### 6.1 合法广告目的

| ad_purpose | 中文 | 说明 |
| --- | --- | --- |
| `traffic` | 引流 | 获取曝光、点击、搜索词样本和词根方向 |
| `conversion` | 转化 | 承接有效词、提升订单、稳定 CVR |
| `ranking` | 冲排名 | 围绕主推词或核心词推动自然排名 |
| `profit` | 利润 | 控制 ACOS / TACOS，提高广告效率 |

### 6.2 合法广告方向

| ad_direction | 中文 | 说明 |
| --- | --- | --- |
| `expand_keywords` | 扩词/引流 | 新增关键词、自动/广泛探索、搜索词提取 |
| `push_natural_rank` | 推自然位 | 主推词提 Bid、预算不断供、必要时 TOS |
| `optimize_acos` | 优化 ACOS | 否词、降 Bid、降预算、广告位优化 |
| `balance_maintain` | 平衡维持 | 不做策略性大动作，保留核心词和稳定承接 |

### 6.3 目的与方向映射

| ad_purpose | allowed_ad_direction |
| --- | --- |
| `traffic` | `expand_keywords`, `balance_maintain` |
| `conversion` | `expand_keywords`, `push_natural_rank`, `balance_maintain` |
| `ranking` | `push_natural_rank`, `balance_maintain` |
| `profit` | `optimize_acos`, `balance_maintain` |

约束：`balance_maintain` 是广告方向，不是广告目的；不得输出 `ad_purpose=maintain`。

---

## 7. 动作合法性 Ontology

| action_code | 作用对象 | 是否增长型 | 必需证据 | 禁止对象/场景 |
| --- | --- | --- | --- | --- |
| `bid_up_for_ranking` | `KeywordTarget` / `ProductTarget` | 是 | 自然排名、ACOS、CVR、预算利用率、库存、退货评分 | SearchTerm、库存缺失、库存不足、高退货、低评分、缺排名数据 |
| `bid_down_for_acos` | `KeywordTarget` / `ProductTarget` / `AutoTarget` | 否 | ACOS、点击、订单、自然位影响 | SearchTerm、承担 Ranking 任务且未超容忍上限 |
| `campaign_budget_increase` | `Campaign` | 是 | 预算利用率、ACOS、订单、库存 | 库存缺失、库存不足、高退货、核心 SKU 短缺 |
| `campaign_budget_decrease` | `Campaign` | 否 | ACOS 趋势、花费、订单、预算利用率 | 样本不足时大幅降预算 |
| `add_negative_search_term` | `SearchTerm -> NegativeTarget` | 否 | 搜索词点击、花费、订单、ACOS、相关性、标题、后台ST、历史转化 | 精准广告；高相关词；标题词；后台ST词；历史转化词 |
| `promote_search_term_to_keyword` | `SearchTerm -> KeywordTarget` | 是 | 搜索词订单、CVR、ACOS、相关性、词根 | 库存不足、高退货、低评分、无相关性 |
| `placement_adjustment` | `Placement` | 是 | Placement report、ACOS、订单、点击、库存、评分退货 | 广泛/词组/自动广告、缺广告位数据 |
| `eliminate_to_low_bid_pool` | `Campaign` | 否 | 7天花费、订单、CVR、自然位支持、诊断路径 | 广泛/词组/自动活动、新活动<3天、核心词排名支持、未完成淘汰前诊断；目标组必须为 `low_bid_retention_group` |
| `balance_maintain_no_action` | `ASIN` / `Campaign` | 否 | 指标稳定、预算正常、库存支持 | 数据缺失导致无法判断时不得伪装为健康维持 |

---

## 8. 报表与证据 Ontology

| report_or_signal | 中文 | 可证明什么 | 可触发动作 |
| --- | --- | --- | --- |
| `search_term_report` | 搜索词报告 | 用户实际查询、搜索词订单、搜索词花费、搜索词 ACOS | 否词、提词、新增精准承接 |
| `targeting_report` | 投放目标报告 | 关键词、商品目标或自动目标表现 | Bid 调整、目标保留、目标降级 |
| `advertised_product_report` | 被广告商品报告 | 不同 ASIN/SKU 广告表现 | 子体承接判断、核心 SKU 风险判断 |
| `placement_report` | 广告位报告 | TOS / RoS / PP 的曝光、点击、订单、ACOS | 广告位加价、广告位降价、阻断广告位动作 |
| `inventory_status` | 库存状态 | 父 ASIN 与核心 SKU 是否支持增长 | 阻断或降级增长型动作 |
| `organic_rank_signal` | 自然排名信号 | Ranking 是否有机会或是否正在下滑 | 推自然位、保护主推词、阻断误降 Bid |
| `listing_quality_signal` | Listing 承接信号 | 评分、退货、页面转化是否支持放量 | 阻断扩词、阻断 TOS、转为诊断型 Traffic |
| `browse_node_context` | 类目节点上下文 | 类目竞争、类目 CPC、类目季节性基准 | 阈值选择、品类均值选择 |
| `product_type_definition` | 商品类型定义 | Listing 必填属性和条件属性是否完整 | Listing 诊断、数据质量提示 |

---

## 9. 推理与校验规则

| rule_id | 推理规则 |
| --- | --- |
| ONT-001 | 若对象为 `SearchTerm`，则允许 `add_negative_search_term` 或 `promote_search_term_to_keyword`，禁止直接 `bid_up` / `bid_down`。 |
| ONT-002 | 若 `campaign_type in [broad_keyword, phrase_keyword, auto_discovery]`，则 `placement_adjustment = N/A`。 |
| ONT-003 | 若 `campaign_type=exact_keyword` 且 `ad_purpose=ranking` 且 `organic_rank_signal` 缺失，则阻断 `push_natural_rank`，建议转 `conversion` 或 `balance_maintain` 方向观察。 |
| ONT-004 | 若 `inventory_status` 缺失，则所有增长型动作 BLOCKED；非增长型动作也必须标记数据质量风险。 |
| ONT-005 | 若 `placement_report` 缺失，则仅阻断 `placement_adjustment`，不阻断关键词 Bid、预算、否词等非广告位动作。 |
| ONT-006 | 若 `ProductTypeDefinition` 显示 Listing 必填属性缺失，且 CVR 显著低于品类均值，则优先输出 Listing 承接诊断，不做激进扩词。 |
| ONT-007 | 若 `BrowseNode` 或品类基准缺失，则使用账户均值作为保守降级，并提升审核等级。 |
| ONT-008 | 若 `ad_direction=balance_maintain`，不得自动推导 `ad_purpose=maintain`。广告目的保持原值或在 traffic / conversion / ranking / profit 内建议切换。 |
| ONT-009 | 若自动投放 `AutoTarget` 跑出有效 SearchTerm，则优先 `promote_search_term_to_keyword` 新增 EXACT 或 long_tail 承接，而不是直接持续提高自动广告 Bid。 |
| ONT-010 | 若 ProductTarget 的 PP 表现优于 TOS / RoS，则允许 PP 方向加价；但仍需通过库存、退货、评分和预算护栏。 |
| ONT-011 | `Portfolio` 是广告组织对象，不等于 `AdBudgetGroup`；二者不得混用。 |
| ONT-012 | `Campaign` 必须且只能归属 1 个 `AdBudgetGroup`。 |
| ONT-013 | 若 `action_code=eliminate_to_low_bid_pool`，则目标组必须为 `low_bid_retention_group`。 |
| ONT-014 | `low_bid_retention_group` 仅允许精准活动，组合预算固定 $1、活动 Bid 固定 $0.20，且不参与增长预算分配。 |
| ONT-015 | 精准广告禁止创建否定词；只能调整 Target Bid、Campaign 预算/广告位或关闭。 |
| ONT-016 | 广泛/词组/自动广告需要退出时关闭，不得迁入 `low_bid_retention_group`。 |
| ONT-017 | 同一 Campaign 同时包含精准与非精准 Target 时，组合路由 BLOCKED；Agent 建议按匹配类型拆分，人工确认后再执行。 |

---

## 10. 建议后续对接点

| 对接文件 | 建议对接方式 |
| --- | --- |
| `02-标签维度定义.md` | 引用本文第 6 节，锁定广告目的与广告方向枚举 |
| `05-动作规则.md` | 引用本文第 7 节，补充每个动作的作用对象和禁止对象 |
| `07-广告位规则.md` | 引用本文第 4 节和第 9 节，确保广泛/词组/自动广告不输出 Placement 调整 |
| `10-安全护栏.md` | 引用本文第 9 节，把对象级非法动作作为硬校验 |
| `12-输出规范.md` | 引用本文第 3 节和第 7 节，输出必须包含 object_class、campaign_type、action_code |
| `17-问题诊断与动作优先级.md` | 引用本文第 8 节，把问题诊断与证据源绑定 |
| `18-广告执行调整流程.md` | 在 Campaign Type 判断后加入 ontology 合法性校验 |
| `23-广告组合与预算分配规则.md` | 对齐 ParentASIN / AdBudgetGroup / Campaign 的预算控制关系 |

---

## 11. 建议新增输出字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `object_class` | enum | ParentASIN / Campaign / AdGroup / KeywordTarget / ProductTarget / AutoTarget / SearchTerm / Placement |
| `campaign_type` | enum | exact_keyword / phrase_keyword / broad_keyword / product_targeting / auto_discovery |
| `targeting_type` | enum | keyword / product / auto / negative |
| `match_type` | enum | EXACT / PHRASE / BROAD / N/A |
| `is_growth_action` | bool | 是否增长型动作 |
| `ontology_validation_status` | enum | passed / blocked / downgraded / review_required |
| `ontology_rule_refs` | list | 命中的 ontology 规则，如 ONT-001 |
| `source_report_refs` | list | 使用的数据源，如 search_term_report / placement_report |
| `budget_group_type` | enum | exact_core_group / exact_testing_group / auto_broad_group / low_bid_retention_group |
| `budget_group_name` | string | 当前所属预算组名称 |
| `portfolio_id` | string | Portfolio 标识 |
| `portfolio_name` | string | Portfolio 名称 |
| `released_at_campaign_level` | number | 转低价捡漏在活动层释放的预算 |
| `portfolio_budget_summary_ref` | string | 关联的组合预算回算摘要 |
