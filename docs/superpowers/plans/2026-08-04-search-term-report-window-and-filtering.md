# 搜索词报告窗口与知识库驱动筛选改造方案

> **供实施 Agent 使用：** 按任务逐项执行并勾选；实施前先阅读本文列出的知识库规则。不得恢复旧的 `Top 10 + 仅花费排序` 截断。本期单活动 LLM 词数上限定为 **20**；它是技术容量保护，排序规则另行明确，不能伪装成知识库业务阈值。

**目标：** 让广泛/词组/自动活动的搜索词分析以知识库的样本与动作规则为准：先完成活动基础数据读取，再用活动既有的 7 天基线决定是否需要搜索词报告；活动样本不足时不取、不透传搜索词，且不产生搜索词来源的否词或扩词；样本充足时统一取得 7 天和 14 天报告。进入 LLM 的词集以 7 天报告为唯一基线：先丢弃无可用信号的低点击/低曝光/无订单词，再按“订单数降序、花费降序、点击数降序”排序，最多取前 20 个；只有入选且 7 天样本不足的词才有资格从 14 天报告补充数据，并携带 7 天样本不足布尔值。14 天只能按同一标准化词键补充，不能新增 7 天不存在的词。所有传给 LLM 的窗口和字段必须显式标注，不能用固定证据模板把 14 天数字误写成 7 天证据。

**架构：** 活动基础数据 MCP 是搜索词 MCP 的前置阶段。前置阶段完成后，为每个活动计算 `campaign_sample_insufficient`（默认 `false`，只在 KB17 条件命中时置 `true`）；命中则不调用 `ad_campaign_search_term_report`，也不向 LLM 注入搜索词。未命中的活动再统一调用现有搜索词 MCP 获取 7 天和 14 天，两个窗口都先完整落入内存 bundle。bundle 组装时，先以 7 天行集合、标准化词键和 7 天指标完成清洗，再按订单数、花费、点击数降序排序并截取前 20 个；最终 `terms` 只能来自这批 7 天候选。对入选且 7 天样本不足的词，才用同一标准化词键在 14 天 map 中查找并附加 `metrics_14d`；14 天独有的词、无法匹配的词不能进入 LLM。30 天目前不请求，因为知识库没有定义它在搜索词决策中的用途或阈值；P3 也不产生特殊窗口分支。

**技术栈：** Python 3.13、asyncio、现有 Campaign MCP 适配器、Pydantic v2、pytest。

---

## 一、结论与定夺

### 1. 活动门禁后统一取 7/14 天，逐词决定透传窗口

搜索词 MCP 的入参是活动、店铺、起止日期；它不支持“只查询样本不足的某几个搜索词”。当前 7 天结果出来后，任一活动中几乎都会有大量低点击、低花费的长尾词；以这些词作为 14 天查询触发条件，实质上会让绝大多数活动仍然查询 14 天，却引入不稳定、难解释的分支。

因此本方案锁定为：**先完成活动基础数据 MCP；仅对活动级样本充足的 BROAD/PHRASE/AUTO 活动，统一获取 7 天和 14 天完整搜索词报告。** 同一活动名称在一个批次内去重请求；两个窗口都完整取回，但最终词集以 7 天为唯一基线。7 天行先做空值/低信号清理，再按确定性优先级排序并受单活动技术容量上限约束；7 天未入选的词不再尝试补 14 天。入选的 7 天样本充足词只传 7 天；入选的 7 天样本不足词，才用标准化词键到 14 天结果中查找并在命中时同时传 7 天和 14 天。14 天没有对应 7 天基线词时永远不能新增 LLM 词条。14 天是否被 LLM 作为上下文消费，由提示词中的窗口职责约束；它不能被代码或提示词误当成 7 天事实。

这不是“把所有原始数据交给 LLM”。完整报告是 MCP 到服务端的取数范围；进入提示词的仍是经规则归类后的可审阅事实，并附完整统计，避免无声丢失。

活动级样本不足时则不发起搜索词 MCP，也不创建空报告冒充“无搜索词”。分析主基线本来就是活动 7 天指标；此时搜索词报告通常无法形成可靠的否词或扩词证据。summary 上保留结构化原因 `search_term_fetch_status="SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT"`，但不把任何搜索词文本/指标传入 LLM。活动基础数据读取与搜索词 MCP 读取必须在流程上串行；同一活动不得在活动门禁尚未计算时提前发搜索词请求。

### 2. 30 天当前没有知识库授权的触发规则，不纳入本期

已核对的规则中：

- `执行规则/18-广告执行调整流程.md`：7 天是搜索词沉淀及主要决策窗口；未规定搜索词 30 天动作。
- `执行规则/31-广泛自动词组调整规则.md`：7 天是搜索词、否词和转精准的主窗口；14 天可用于长尾、低流量、样本不足词的延长观察；未定义 30 天搜索词窗口。
- `03-阈值参数.md`：给出 7 天花费阈值的计算方式，未给出 30 天搜索词阈值。

所以不能把“30 天用于稳定性”写成既定规则，更不能据此新增否词、降价或转精准。若业务后续要 30 天数据，必须先在知识库中明确：它回答什么问题、覆盖哪些词、与 7 天冲突时以谁为准、能否触发动作。本期只在数据结构上预留可扩展的 `windows` 容器，不发 30 天 MCP 请求。

### 3. 旧的过滤/截断应整体废弃

当前 `campaign_fetcher.py::fetch_search_terms_for` 的规则是：保留有订单、点击至少 3、或曝光至少 200 且点击不超过 1 的行，之后按花费/曝光取前 10 条。这些阈值和排序均找不到对应知识库依据；它还会把“点击 2 次”的词无条件保留，并把可能形成动作证据的低花费成交词、同根词聚合线索或高花费词静默截掉。

本方案不再以“高曝光”或“仅花费 Top N”作为进入 LLM 的理由，也不设置 6/4/2、Top 10 等旧配额。方案固定设置单活动最多 20 个词的技术容量上限；订单、花费、点击的排序只是确定这 20 个词的优先顺序，必须记录被低信号删除、容量截断和延后审阅的数量与原因，不能伪装成新的业务动作阈值。

## 二、样本不足：知识库事实与适用范围

### 1. 搜索词动作使用 KB31 的口径

针对单个搜索词，应使用 `执行规则/31-广泛自动词组调整规则.md` §4 的分层规则：

```text
样本不足（Tier 1） = 活动上线不足 3 天
                    OR (7 天点击 < 10 AND 7 天花费 < 无单花费门槛)

无单花费门槛 = max($15, 目标 CPA)
```

处理结果是：不做大幅调整；知识库允许长尾、低流量、样本不足词延长至 14 天观察（KB31 §7），但本期不新增产品/活动级特殊窗口。对已通过活动门禁、且已进入 7 天候选集的词，词级样本不足词才有资格从 14 天同键结果附带数据，供 LLM 了解该词的双窗口事实；这不等于允许仅凭 14 天独立触发动作，也不因样本不足而否词。

同一节还定义了无订单但样本已足的后续层级：

```text
Tier 2：相关性 R1/R2，且 7 天点击 >= 10 或花费 >= max($15, 目标 CPA)
        → 降一档，观察 7 天；不直接否词

Tier 3：相关性 R1/R2，且 7 天点击 >= 20 或花费 >= max($20, 目标 CPA × 2)
        → 降两档、人工复核；R1 通常不否词

Tier 4：明显不相关 R3/R4
        → 不设数据阈值，允许精准否词
```

目标 CPA 无法得到时，沿用 `03-阈值参数.md` §7.3 的无单花费降级值，并把 `DATA_MISSING` 与提高复核级别作为证据写入，不把未知值默认为零。

### 2. KB17 的 OR 口径不能直接拿来过滤搜索词

`执行规则/17-问题诊断与动作优先级.md` 写的是活动诊断层的 `SAMPLE_INSUFFICIENT`：活动不足 3 天，**或** 7 天花费低于样本阈值，**或** 点击少于 10。它的作用是活动整体诊断，与 KB31 的搜索词动作层不同。

实现取数门禁时，活动上线天数使用已有 MCP `basic_info.days_online`：只有 `0 <= days_online < 3` 才命中“新活动”分支；`-1` 是未知值，不能误当成新活动。样本花费阈值使用当前 KB03 §7.3 的 `max($5, 目标CPA × 0.5)`；该参数在 KB 中仍标为“需确认”，因此日志须同时记录命中的具体子条件和阈值来源，便于后续运营校准。

`目标CPA` 不新增数据源，复用现有 `CampaignStrategyContext.target_cpa`：在进入分析、搜索词预取之前，代码已按 `平均订单金额 × target_acos` 计算；`target_acos` 的现有优先级是人工覆盖 → P3 推荐值 → 算法推荐，平均订单金额取 ASIN 广告汇总的 `sales / orders`。广告汇总无订单或缺失时该字段为 `None`，取数门禁按 KB03 的 `$5` 下限计算，并记录 `target_cpa_missing`；不得将 `None` 当作零。

两个定义不能混用：本改造对**单个搜索词**采用 KB31 的 “点击不足且花费不足” 组合条件；活动诊断继续保留 KB17 自己的口径。实现中必须把两者命名为 `campaign_sample_insufficient` 与 `term_sample_insufficient`，禁止共享一个含义不清的布尔字段。

本期的使用边界进一步锁定如下：

- `campaign_sample_insufficient=True`：取数门禁。跳过搜索词 MCP，且搜索词来源的 `negative_keywords` 与 `exact_promotion_candidates` 必须为空。
- `campaign_sample_insufficient=False`：请求 7 天和 14 天报告。
- `term_sample_insufficient=True`：该词以 7 天为基线；仅当它已通过 7 天清洗/容量截断且在 14 天按标准化词键命中时，才透传 `7d + 14d` 两个窗口，供 LLM 了解样本延长事实；无 14 天同键时仍可透传 7 天并明确 14 天不可用，但不能仅凭 14 天独立触发动作。
- `term_sample_insufficient=False`：该词只透传 `7d`，14 天已取回但不进入该词的 LLM payload。
- 产品定位 P3 不参与本期窗口选择。KB31 §7 的 14 天用途是长尾、低流量、样本不足词的延长观察，并非 P3 专属分支；本期不新增产品/活动级特殊窗口，也不以 14/30 天替代 7 天基线。

### 3. 只抽活动级公共样本判定 helper

当前 `_sample_insufficient()` 只存在于 `campaign_guardrails.py`，搜索词预取门禁还没有统一实现。应新增 `app/core/campaign_sample.py`，让活动取数门禁和护栏共享“事实判定”，而不是共享一个带隐含副作用的布尔函数。搜索词级样本不足仍在 bundle 组装中按 KB31 单独计算，不进入这个公共 helper。

公共层返回结构化结果，至少包含：

```python
SampleAssessment(
    insufficient: bool,
    reasons: tuple[str, ...],
    scope: Literal["campaign"],
    days_online: int,
    clicks_7d: int | None,
    cost_7d: float | None,
    threshold: float | None,
    data_missing: tuple[str, ...],
)
```

公共层只提供一个明确入口，不提供 `is_sample_insufficient(..., mode=...)` 这种参数分支：

- `assess_campaign_sample(...)`：KB17 活动口径，`days_online` 仅在已知且 `<3` 时命中新活动条件；7 天花费低于 `max($5, target_cpa×0.5)` 或点击少于 10 也命中。`days_online=-1`、缺失数值不能被默认为新活动或零，但已有任一明确不足条件时仍可判定 `True`。

目标 CPA 缺失时使用活动级知识库规定的 `$5` 下限，并在 `data_missing/reasons` 中标记。helper 只返回活动级事实，不修改动作、不清空候选、不发 MCP。

护栏继续保留自己的消费策略：P1 使用 `assess_campaign_sample` 阻止非硬淘汰；现有“测试期且上线 <14 天”的保护必须作为独立 `testing_stage_protection` 原因叠加，不能偷偷并入 KB17 活动样本不足。当前护栏把 `days_online=3` 判为不足（`<=3`）而 KB17 是 `<3`，这是现存口径差异；迁移到公共 helper 时应按 KB17 修正并同步边界测试，除非业务明确要求继续保留旧边界。P3 硬淘汰仍按现有护栏优先级处理，不由 helper 改写。

## 三、目标数据契约

每个活动以完整 MCP 回包为基础，产生如下只读 bundle；根字段始终是 7 天值，避免既有转精准逻辑误把 14 天当作 7 天：

```python
{
    "windows": {
        "7d": {"available": True, "start_date": "...", "end_date": "..."},
        "14d": {"available": True, "start_date": "...", "end_date": "..."},
    },
    "summary": {
        "raw_row_count": 438,
        "normalized_term_count": 421,
        "orders": 9,
        "cost": 142.30,
        "sales": 510.00,
        "term_sample_insufficient_7d_count": 386,
        "dropped_low_signal_count": 12,
        "cap_truncated_count": 38,
        "transmitted_term_count": 371,
        "fourteen_day_match_count": 352,
        "prompt_deferred_count": 0,
    },
    "terms": [
        {
            "keyword": "sticky bra",
            "metrics_7d": {"clicks": 12, "cost": 18.5, "sales": 90, "orders": 3,
                           "impressions": 420, "acos_raw": 20.56, "cvr_raw": 25.0,
                           "sales_corrected": 87.70, "acos_corrected": 21.10, "cvr_corrected": 24.40,
                           "data_min_age_days": 3, "data_maturity": 0.85,
                           "maturity_basis": "uniform_7d_aggregate"},
            "term_sample_insufficient": False,
            "term_sample_insufficient_basis": "7d",
            "review_reason": ["has_order"],
        },
        {
            "keyword": "long tail query",
            "metrics_7d": {"clicks": 4, "cost": 3.0, "sales": 0, "orders": 0,
                           "impressions": 80, "acos_raw": None, "cvr_raw": 0.0,
                           "sales_corrected": None, "acos_corrected": None, "cvr_corrected": None,
                           "data_min_age_days": 3, "data_maturity": 0.85,
                           "maturity_basis": "uniform_7d_aggregate"},
            "metrics_14d": {"available": True, "clicks": 9, "cost": 8.0, "sales": 0, "orders": 0,
                            "impressions": 210, "acos_raw": None, "cvr_raw": 0.0,
                            "sales_corrected": None, "acos_corrected": None, "cvr_corrected": None,
                            "data_min_age_days": 3, "data_maturity": 0.85,
                            "maturity_basis": "uniform_14d_aggregate"},
            "term_sample_insufficient": True,
            "term_sample_insufficient_basis": "7d",
            "review_reason": ["term_sample_insufficient"],
        }
    ],
}
```

规则：

- 同一活动内对标准化后的搜索词聚合，再保留原始 ACOS/CVR 和成熟度校正后的 ACOS/CVR；不把多个原始行任选一行当事实。所有 ACOS/CVR 判定只使用校正值，原始值只能作事实展示。
- `terms` 的候选集合、排序、低信号过滤、容量截断全部只看 7 天；`term_sample_insufficient` 明确表示“按 7 天指标计算的词级样本不足”，不得用 14 天反算或覆盖该布尔值。
- 7 天和 14 天按标准化词键精确 join，不能按数组位置或 14 天自己的排序对齐。14 天调用失败、词不存在或字段缺失时只标记不可用，不补 0；14 天独有的词直接忽略，不创建新的 `terms` 行。只有入选且 `term_sample_insufficient=True` 的 7 天词，在 14 天 map 命中时才携带两个窗口；入选且样本充足词只携带 7 天。
- 低信号清理和容量截断发生在 14 天补充之前；被丢弃或被截断的 7 天词不进行 14 天查找，也不进入 LLM。被截断词只在 summary 中计数，不能静默冒充“14 天无匹配”。
- 归因成熟度必须复用 KB03 §12 的运行时配置和成熟度口径；7/14 天活动级聚合使用 `maturity_basis="uniform_7d_aggregate"` / `uniform_14d_aggregate` 等显式标记。校正值为 `None` 时视为证据不足，不得用原始值或无穷大替代。
- 摘要统计始终基于完整标准化报告计算，不能基于最终传入 LLM 的 `terms` 反推。
- 现有下游若仍需要 `_search_term_data` 的列表形式，先通过显式适配层读取 `bundle["terms"]`；不得让列表和 bundle 两种语义在同一个字段下混用。
- 活动级样本不足时不生成这个 bundle；summary 只带 `search_term_fetch_status`，不得以 `terms=[]` 暗示 MCP 已成功返回空报告。

## 四、提示词窗口与证据协议

搜索词 bundle 不能再以“搜索词报告（N 个词）”的无窗口列表直接渲染。每个进入 payload 的词必须携带 `term_sample_insufficient=<true|false>`，该值明确是 7 天基线判定；每个指标必须携带窗口标签：7 天样本充足词只出现 `[window=7d]`；7 天样本不足词在 14 天同键命中时同时出现 `[window=7d]` 与 `[window=14d]`。14 天已取回但不满足词级透传条件、或 14 天没有同键命中时，不得在该词文本中隐式出现。

提示词必须动态解释字段口径，而不是用固定业务句式代替数据：

- `impressions`、`clicks`、`cost`、`sales`、`orders`：指定窗口内该搜索词的报告值；不能把 7 天和 14 天相加后继续称作 7 天。
- `acos_raw` / `cvr_raw`：报告原始值，只作事实展示；`acos_corrected` / `cvr_corrected`：按归因成熟度校正后的值，只有校正值可用于动作判断。
- `data_maturity`、`data_min_age_days`、`maturity_basis`：说明数据成熟度、最新数据龄期和聚合校正方式；校正值为 `None` 表示证据不足，不是零销售或无限 ACOS。
- `term_sample_insufficient`：只表示该词按 7 天点击/花费/上线天数规则判定的样本不足；它不是 14 天是否有行、也不是活动级 `campaign_sample_insufficient`。
- 7 天是主动作窗口；14 天是同一搜索词的辅助窗口，只能在该词实际携带 14 天字段时引用。

LLM 的证据要求：

- 证据中凡出现点击、花费、订单、销售额、ACOS、CVR 等数字，必须同时写明 `7d` 或 `14d`；同时使用两个窗口时分别列出，不能合并成一个未标窗数字。
- 不使用固定的“7 天花费……所以……”证据模板；renderer 只提供当前 payload 的窗口标签和字段字典，证据内容由 LLM 根据实际存在的窗口生成。
- 不允许把缺少 14 天字段的词推断成“14 天无数据”，也不允许把 14 天数值改写成 7 天证据。后端候选事实仍由 `reasoner.py` 按词回填，LLM 的证据文字不能覆盖事实字段。

## 五、知识库驱动的“进入审阅”原则

这里的筛选顺序固定为：**7 天基线清洗 → 单活动容量截断 → 对入选的 7 天样本不足词补 14 天**。14 天不能反过来决定一个词是否进入 LLM。

1. **先删除纯低信号词：** 空词、无法标准化的词直接删除。对已标准化词，仅当 7 天同时没有可用行为信号（订单=0、点击=0、花费=0，且曝光=0 或曝光字段缺失）时删除；这覆盖“低点击、低曝光、无订单且没有任何行为证据”的词。知识库没有授权 `曝光<200`、`点击<=1` 等固定数值作为删除线，因此不得恢复旧阈值。存在任一非零信号的词保留为候选，并由 `term_sample_insufficient` 标记其 7 天样本状态。
2. **建立 7 天唯一候选集：** 同一活动按标准化词键聚合；7 天结果中没有出现的词永远不因 14 天报告而新增。14 天数组顺序、额外词数和排序均不影响候选集。
3. **按单活动技术容量上限截断：** 使用显式配置 `search_term_llm_max_terms_per_campaign=20`。候选排序只使用 7 天事实，按 `orders desc, cost desc, clicks desc` 依次排序；完全相同的前三项再用 `impressions desc, keyword asc` 稳定排序。取排序后的前 20 个，未入选词不做 14 天行级 join/附加、不进入 LLM，`cap_truncated_count` 必须可观测；这不影响活动级 14 天 MCP 已经按统一策略完成取数。
4. **只对入选的 7 天样本不足词补 14 天：** 用标准化词键在 14 天 map 中查找；命中才附 `metrics_14d`，未命中保持 `14d.available=False`，不补零、不创建词条。入选的 7 天样本充足词只携带 7 天。
5. **动作语义：** 7 天样本充足词和样本不足词都只是 LLM 审阅候选，不代表直接转精准。7 天是所有动作的量化基线；14 天只提供长尾/低流量/样本不足词的辅助观察事实，不能独立触发否词、转精准或出价动作。低信号被删除的词不进入 LLM，也不能由 LLM 生成搜索词来源动作。

这里的“低点击/低曝光/无订单”只在没有任何可用行为信号时作为删除条件；不能把“无订单”单独当成不相关，也不能擅自把 `点击<=1`、`曝光<200` 等旧数值重新包装成新业务规则。若后续确实要删除带有少量点击或曝光、但仍未达动作门槛的词，必须先在知识库或配置中明确对应阈值；在此之前它们保留为 7 天样本不足候选，并按容量上限竞争 LLM 名额。

以上规则完全移除旧的“曝光 >= 200 且点击 <= 1”“点击 >= 3 即保留”和“花费 Top 10”逻辑；它们既不是知识库的样本不足定义，也不是本期的技术容量配置。

### 上下文容量的诚实处理

每活动容量上限是唯一的逐词截断点，当前固定配置为 20，必须在 bundle 和 prompt 观测中记录：原始 7 天词数、低信号删除数、容量截断数、最终传输数、其中 7d-only/7d+14d 数量。若单词条仍因 renderer 估算超出模型上下文预算，不能静默继续截尾；应将该活动降级为“仅汇总、无逐词动作”，记录 `prompt_deferred_count` 和原因。后续可依据脱敏生产样本和实际 renderer token 测量调整该技术值，但不得改变知识库动作阈值。

这条容量保护是技术失败闭环，不是新的业务过滤规则。

## 六、实施任务

### 任务 1：锁定知识库口径和纯数据分类

**文件：**

- 修改/恢复：`ad-direction-agent/app/core/attribution_maturity.py`（当前 checkout 未找到该 KB03 §12 引用的模块；先补齐或从已确认基线恢复，不能假设它已存在）
- 新建：`ad-direction-agent/app/core/campaign_sample.py`（活动级结构化样本判定）
- 修改：`ad-direction-agent/app/config/settings.py`（增加 `search_term_llm_max_terms_per_campaign: int = 20`；这是技术容量配置，不是业务阈值）
- 修改：`ad-direction-agent/app/data/campaign_fetcher.py`
- 修改：`ad-direction-agent/app/workflow/steps/campaign_guardrails.py`（消费活动级 helper；保留测试期特殊保护为独立原因）
- 新建：`ad-direction-agent/tests/data/test_campaign_search_term_bundle.py`
- 修改：`ad-direction-agent/tests/workflow/test_campaign_guardrails.py`
- 修改：`docs/knowledge_base/执行规则/31-广泛自动词组调整规则.md`（仅补充“活动级取数、7/14 窗口职责”说明，不改业务阈值）

- [ ] 先验证/补齐 `attribution_maturity` 的配置读取、成熟窗门禁和聚合窗校正接口；配置来自 `thresholds.toml [attribution_maturity]`，不得在搜索词 bundle 中重写曲线常量。若校正值因未成熟或数据缺失为 `None`，bundle 只保留原始事实并标记 `data_maturity`，任何依赖 ACOS/CVR 的候选必须降级为不判定。
- [ ] 先为 `campaign_sample.py` 写活动级边界测试：`days_online=2/3/4/-1`、点击 `9/10`、花费低于/等于/高于 `$5`/目标 CPA 阈值；确认默认 `false`、未知值不被当成新活动或零。搜索词级 `clicks<10 AND cost<no_order_cost` 仅在 bundle 测试中覆盖，不进入公共 helper。
- [ ] 实现 `assess_campaign_sample`，返回结构化 `SampleAssessment`；将护栏的 `_sample_insufficient` 改为消费活动级 helper，测试期 `<14 天` 作为独立原因，不与 KB17 布尔值混合。
- [ ] 为标准化、同词聚合、7 天完整汇总、纯低信号删除、7 天唯一候选集、单活动容量截断、KB31 `term_sample_insufficient`、7 天/14 天透传选择、14 天不可用值、原始/校正 ACOS/CVR 及成熟度标记写失败测试。测试必须覆盖：7 天词集合与 14 天词集合不一致时只保留 7 天词；7 天样本不足且 14 天同键命中时携带双窗口和布尔值；14 天无同键时不补零；低信号词计入 `dropped_low_signal_count`；超过容量的词计入 `cap_truncated_count` 且不查 14 天。原有 `clicks=9/cost<no_order_cost`、`clicks=9/cost>=no_order_cost`、`clicks=10`、`orders=1`、空词及重复词继续覆盖。
- [ ] 抽出纯函数 `_build_search_term_bundle(rows_7d, rows_14d, *, campaign_online_days, target_cpa, maturity_context, max_terms_per_campaign)`：只做解析、聚合、join、低信号过滤、7 天排序/容量截断、规则标记和成熟度校正；不发 MCP、不调用 LLM。`terms` 只能由 `rows_7d` 生成，14 天只能作为入选 7 天不足词的同键补充。
- [ ] 数值阈值从既有策略上下文/KB03 参数来源计算；无法取得目标 CPA 时，输出显式数据质量标记并走知识库的保守降级值。
- [ ] 运行：`python -m pytest tests/data/test_campaign_search_term_bundle.py -q`，预期全部通过。

### 任务 2：以活动级样本不足为门禁，预取固定 7 天 + 14 天

**文件：**

- 修改：`ad-direction-agent/app/data/campaign_fetcher.py`
- 修改：`ad-direction-agent/app/workflow/steps/campaign.py`
- 新建：`ad-direction-agent/tests/workflow/test_campaign_search_term_prefetch.py`

- [ ] 写测试：`campaign_sample_insufficient=True` 的 BROAD/PHRASE/AUTO 活动不发起任何搜索词 MCP，保留 `search_term_fetch_status="SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT"`，且无 `_search_term_data`；样本充足的活动恰好发出两次同一 MCP 工具调用，日期窗口分别是站点本地时间的滚动 7 天、14 天；相同活动名只请求一次；EXACT 不请求。两次回包故意使用不同词集合和顺序，断言最终候选仍以 7 天为准，14 天独有词不会进入 LLM；构造超过 20 个 7 天候选，断言只保留排序前 20 个。
- [ ] 将 `fetch_search_terms_for` 拆成“单窗口请求”与“活动 bundle 组装”两个职责。活动基础数据 MCP 完成并计算门禁后，再用 `asyncio.gather` 同时请求该活动的 7 天和 14 天；两类请求都复用现有 `self._mcp_sem`，不新建并发池、不新增 MCP 工具。
- [ ] 在 `_prefetch_search_terms` 入口、任何搜索词 MCP 调用前调用 `assess_campaign_sample`。命中时只写 fetch status；未命中时才发起两个窗口请求，传入活动上线天数与目标 CPA，并把 bundle 挂回相应 summary。
- [ ] 14 天调用失败时保留完整 7 天 bundle，`windows["14d"].available=False`，记录 warning；7 天调用失败时该活动不产生搜索词逐词建议，但不阻断整个广泛流。
- [ ] 运行：`python -m pytest tests/workflow/test_campaign_search_term_prefetch.py -q`，预期全部通过。

### 任务 3：明确提示词契约，阻断样本不足活动的搜索词动作

**文件：**

- 修改：`ad-direction-agent/app/llm/reasoner.py`
- 修改：`ad-direction-agent/app/models/campaign.py`（候选对象承载 reasoner 回填的 7 天校正指标/成熟度字段）
- 修改：`ad-direction-agent/app/workflow/steps/campaign_search_term_promotion.py`（仅在确定性准入需要读取校正 ACOS 时，读取候选上已由 reasoner 回填的 7 天校正字段；不承担报告回填）
- 修改：`ad-direction-agent/tests/workflow/test_campaign_search_term_promotion.py`

- [ ] 写测试：活动 fetch status 为 `SKIPPED_CAMPAIGN_SAMPLE_INSUFFICIENT` 时，提示词明确该活动仅观察，要求 `negative_keywords=[]` 与 `exact_promotion_candidates=[]`；样本充足活动中，7 天样本充足词只渲染 `window=7d`，入选且 7 天样本不足、14 天同键命中的词同时渲染 `window=7d` 与 `window=14d`，每个词都渲染 `term_sample_insufficient` 布尔值；14 天独有词、未入选词和无同键词均不渲染；证据中的数字必须带窗口标签，且缺失的 14 天不显示成 0。
- [ ] 将 prompt renderer 改为读取 bundle 与 fetch status。活动级样本不足时只渲染观察原因，不渲染搜索词区块，并禁止 LLM 为该 cid 输出搜索词来源的否词/转精准候选。其他活动逐词指标明确标注 `7d`/`14d`，动态注入字段口径和证据窗口要求；不得使用固定的“7 天花费/订单……”句式代替实际窗口。
- [ ] 保持现有职责：由 `reasoner.py` 在 LLM 候选通过 `cid + search_term` 校验后，从 bundle 的 `metrics_7d` 回填 `clicks/orders/cost/sales`、校正后的销售/ACOS/CVR 及成熟度字段；`campaign_search_term_promotion.py` 继续只负责 R1 过滤、去重、订单/词根通道准入，并读取候选上已回填的校正字段计算准入，不移动回填逻辑。聚合通道若缺少校正销售/ACOS，必须降级为不判定，不能用 raw ACOS 代替。词级 7d/14d 透传状态由 bundle 内部按 KB31 计算，不在 reasoner 内重复写阈值。
- [ ] 对无 bundle 的样本不足活动，reasoner 的候选归一化与下游准入共同拒绝搜索词来源候选；禁止误读 14 天指标或将缺失搜索词当作 0。
- [ ] 若 renderer 命中技术上下文预算，返回该活动的汇总与显式延后标记，不产生任何来自被延后逐词列表的建议。
- [ ] 运行：`python -m pytest tests/workflow/test_campaign_search_term_promotion.py -q`，预期全部通过。

### 任务 4：观测、回归与 30 天决策门

**文件：**

- 修改：`ad-direction-agent/app/workflow/steps/campaign.py`
- 修改/新建：相关搜索词预取与 prompt 渲染测试
- 修改：本方案及相应交接文档

- [ ] 增加结构化观测：活动级样本不足命中数、跳过 MCP 数、helper 判定 scope/reasons/threshold、每个已取数活动的 7 天/14 天原始行数、标准化词数、低信号删除数、容量截断数、7d-only 与 7d+14d 透传词数、词级 7d 样本不足数量、14 天同键命中数/可用性、提示词容量延后数量及原因；日志不得输出完整搜索词报告。
- [ ] 用脱敏生产样本记录候选数与实际 prompt token 分布，验证两次活动级查询在现有 `campaign_st_fetch_timeout` 下的总耗时；不根据猜测修改超时时间。
- [ ] 运行：`python -m pytest tests/data/test_campaign_search_term_bundle.py tests/workflow/test_campaign_search_term_prefetch.py tests/workflow/test_campaign_search_term_promotion.py -q`，再运行现有 campaign/broad-flow 回归；将预存失败与新失败分开记录。
- [ ] 在业务规则补齐 30 天的用途前，增加测试断言：搜索词预取不会发送 30 天请求，且任何 30 天字段均不能进入动作判断。

## 七、验收标准

- 活动基础数据 MCP 完成后才计算活动级门禁；样本不足严格按 KB17 计算并跳过搜索词 MCP；样本充足活动稳定同时获得 7 天与 14 天两份完整报告；不存在“先看某个词再决定是否取 14 天”的分支。
- 单个搜索词样本不足严格按 KB31 §4 计算，且只影响逐词窗口透传：足样本词 7d-only，不足样本词 7d+14d；活动诊断与搜索词动作的定义不混淆。
- 护栏和搜索词取数门禁消费同一个活动级公共 helper；词级 KB31 仍在 bundle 内部独立计算，活动级 KB17 与搜索词级 KB31 不混用，测试期特殊保护不污染活动级样本判定。
- 旧的 `点击 >= 3`、`曝光 >= 200`、按花费前 10 条逻辑已删除；当前唯一截断是每活动最多 20 个的技术容量上限，按订单数、花费、点击数降序确定优先级，不作为业务动作阈值。
- 7 天继续是所有转精准、否词、出价动作的唯一量化决策窗口；14 天按 KB31 §7 仅作为长尾、低流量、样本不足词的辅助观察事实，且只能补充已入选的 7 天词，不形成独立动作授权；本期没有 P3 专属分支、30 天搜索词请求或隐性动作。
- MCP 原始大报告不会直接注入 LLM；规范化后的 7 天基线候选先经过低信号过滤和单活动容量截断，再按 7d-only / 7d+14d 窗口规则透传。每个词携带 7 天样本不足布尔值，所有被低信号删除、容量截断或因 14 天无同键而未补充的情况均有结构化计数，不能静默遗漏。
- 不新增数据表、Pending 表、异步 MCP、LLM 调用或外部服务；仅复用既有搜索词报告 MCP 与广泛流接线。
