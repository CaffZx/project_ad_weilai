# SkillOpt 接入评估

## 1. SkillOpt 是什么

微软开源项目（[arxiv 2605.23904](https://arxiv.org/abs/2605.23904)，MIT 协议），核心理念：**把 Agent 的 Skill 文档（系统提示词/知识库/记忆）当作可训练参数来优化，不动模型权重。** 纯外挂，离线运行，不修改也不注入生产环境的系统提示词。

训练循环：`Rollout → Reflect → Aggregate → Select → Update → Evaluate`，产出 300-2000 token 的 `best_skill.md`，部署时零额外推理开销。v0.2.0 新增 SkillOpt-Sleep 夜间离线自进化引擎。

## 2. 本项目可优化的对象

| 优化对象 | 对应位置 | 效果预期 |
|---------|---------|---------|
| 系统提示词 | `app/llm/reasoner.py` 的 `EXACT_PROMPT` / `BROAD_PROMPT` / `NEW_CAMPAIGN_PROMPT` 等 | LLM 更少犯规、更准判淘汰/调整/新增 |
| 知识库 | `docs/knowledge_base/` 下 20+ 个 KB 文档 | 规则表述不易被 LLM 误解、边界更清晰 |
| 记忆系统 | 计划中的 campaign 操作记忆 | Agent 从历史经验中学习决策模式 |

## 3. 接入方式

写一个自定义 benchmark：`skillopt/envs/ad-campaign/`

### 3.1 文件清单

```
skillopt/envs/ad-campaign/
├── __init__.py               # 注册 benchmark，5 行
├── initial.md                # 种子 skill：仅含"允许优化"的部分
├── frozen/                   # 只读：SkillOpt 不允许编辑的文件
│   ├── kb-15-策略约束值规则.md
│   ├── kb-16-新增活动规则.md
│   ├── kb-21-淘汰广告活动规则.md
│   └── ...                   # 其余 KB 文档
├── golden_examples.md        # 运营提供的正确调整示例（含对应 KB 引用）
├── dataloader.py             # 数据加载：548 ASIN → 80/20 train/val split
├── rollout.py                # 执行分析 + 拼接 frozen KB + 返回分数
└── scorer.py                 # LLM-as-judge 评分逻辑（核心）
```

### 3.2 评分方案：LLM-as-Judge with Golden Examples

**核心思路**：不用间接信号（warnings 数、执行成功率等），而是由运营提供若干"正确答案"作为 golden examples，让 SkillOpt 的 judge LLM 来逐条评估历史 card 是否遵循 KB 原则、是否和正确示例一致。

#### 为什么不用间接信号

| 被否决的维度 | 原因 |
|-------------|------|
| KB 合规率（warnings 计数） | `_resolve_budget_conflicts` 只抓 egregious 违规（bid≤0.10/budget≤1.01 强制淘汰），大量"形式上合规但逻辑不对"的 case 不会触发 warning。合规不等于正确。 |
| 覆盖完整性（skipped 活动数） | 活动被跳过是**数据源问题**（MCP 空返回、多关键词延迟），不是 LLM 能控制的。放 scorer 里会把上游数据质量噪声误判为 skill 质量。 |
| 运营采纳/驳回率 | 项目刚上线，运营在逐步扩大使用范围和程度，时间趋势会带来严重误导——不是 skill 变好了，是运营变信任了。波动大、不稳定。 |

#### 可保留的信号

| 维度 | 说明 |
|------|------|
| 风控率 | `review_level=SENIOR_APPROVAL` 的占比——风险评级过高说明 LLM 过度谨慎，过低说明盲目自信。合理区间而非极端值更健康。 |
| 执行成功率 | 可保留但需降权。只要 MCP 没挂、ERP 落库正常，这个指标大部分时候是 100%，区分度低。 |

#### Golden Examples 方案（核心）

**数据准备**（人工一次）：

```
golden_examples.md:
  - 示例 1: 精确活动，ACOS 28% > 目标 25%×1.1，近7天有2单，自然排名第8位且上升 → 应降 Bid 而非淘汰（KB 21 §2 自然排名保护）
  - 示例 2: 广泛活动，7天花费$18，CVR=0，无自然排名支撑 → 应淘汰至低价捡漏组（KB 21 §1 NO_CVR_HIGH_SPEND）
  - 示例 3: 新活动上线2天，预算=$1，ACOS 偏高 → 应 keep 观察（KB 21 §2 新活动保护）
  - ...
  建议 10-20 条，覆盖：精准×广泛、淘汰/调整/保持、新活动保护、自然排名保护、预算触底等典型场景
```

**scorer.py 逻辑**：

```python
def evaluate(cards, decision_context, golden_examples, kb_docs):
    """
    对每个 card，让 judge LLM 回答三个问题：
    1. 这个调整是否符合 golden examples 中某条的 pattern？（匹配度 0-1）
    2. 这个调整是否遵守了 KB 规则？（逐条引用 KB 编号）
    3. 这个调整是否有明显错误或遗漏？（如该淘汰的没淘汰、不该动广告位的动了）

    返回三个维度的平均分
    """
```

**为什么这比间接信号好**：

- **有 anchor**：golden examples 是运营认可的正确答案，judge 有参照物
- **覆盖隐性质量**：不只测"不出错"，测"做得对不对"
- **可迭代**：运营可以随时补充新 example、修正旧 example，无需改代码
- **与 rollout 耦合**：每次 skill 变更后，用同一个 golden set 重评，分数变化反映 skill 质量变化

**成本**：548 ASIN × 平均 30 card = ~16000 次 judge 调用。按每次 500ms、并行 10 路估算，一轮评估约 13 分钟。可接受。

### 3.3 数据来源

| 数据 | 来源 |
|------|------|
| 任务列表（548 ASIN） | 服务器 `/opt/ad_agent_github_chenv31/logs/<date>/batch_status.json` |
| 历史决策 cards | ERP `t_advert_agent_modify_suggest_card` |
| 每条 card 的上下文（perf_7d/natural_rank/days_online 等） | ERP `t_advert_agent_modify_suggest_card.perf_json` + card 字段 |
| Golden examples | 人工整理，10-20 条，存储为 `golden_examples.md` |
| KB 文档 | `docs/knowledge_base/` 下执行规则文件 |

### 3.4 起步策略

1. 运营整理 10-20 条 golden examples + 标注对应的 KB 条款
2. 离线回放：用历史 decision 的 card + judge LLM 跑一轮评分，看分数分布
3. 人工抽查极端分（最高/最低）的 case，验证 judge 是否靠谱
4. 评分可信后，接入 SkillOpt 训练循环，开始优化 prompt/KB
5. 每轮训练后用同一个 golden set 评估，验证集分数提升才接受新 skill
6. 长期接入 SkillOpt-Sleep 夜间自进化

### 3.5 细粒度编辑控制：只许改某一部分

SkillOpt 的优化器只编辑 `initial.md` 这一个文件。利用这一点，可以精确控制哪些内容可被优化、哪些永不可改：

**原理**——SkillOpt 的编辑作用域 = `initial.md`，其他内容在 `rollout.py` 中作为只读背景拼接。

```
initial.md              ← 只放"允许优化"的部分
                          例：reasoner.py 的 prompt + 记忆模板

rollout.py              ← 每次 rollout 时从 frozen/ 目录
                          读取 KB 文档，拼接在 initial.md 后面
                          这部分 SkillOpt 完全不可见、不可编辑

frozen/kb-*.md          ← KB 文档原样存放，永不被 SkillOpt 触碰
```

**rollout.py 关键逻辑**：

```python
async def rollout(task, skill_md):
    # skill_md = 本轮 SkillOpt 优化后的 initial.md（仅含 prompt + 记忆）
    
    # 拼接冻结的 KB 文档（不来自 skill_md，SkillOpt 不可编辑）
    frozen_kb = ""
    for f in sorted(Path("frozen").glob("kb-*.md")):
        frozen_kb += f.read_text() + "\n"
    
    # 组装完整的 system prompt
    full_prompt = skill_md + "\n" + frozen_kb
    #                   ↑ 可变           ↑ 永不变
    
    result = await run_analysis(task, full_prompt)
    return {"score": scorer.evaluate(result)}
```

**更细粒度控制**——`initial.md` 内部也可以用标记分区，在 scorer 中加后处理拦截：如果编辑越界碰到了不该改的区域，直接拒绝该候选：

```markdown
<!-- EDITABLE:START -->
（系统提示词 + 记忆模板——SkillOpt 可以改这里）
<!-- EDITABLE:END -->

<!-- FROZEN:START -->
（知识库——SkillOpt 不可改）
<!-- FROZEN:END -->
```

**典型场景配置**：

| 场景 | initial.md 内容 | frozen/ 内容 |
|------|----------------|-------------|
| 只优化记忆系统 | 记忆模板 | prompt + 全部 KB |
| 只优化提示词 | reasoner.py prompt | 全部 KB + 记忆 |
| 同时优化 prompt + 记忆 | prompt + 记忆模板 | 全部 KB |
| 全量优化 | prompt + 记忆 + KB | （空） |

## 4. 核心风险

- **Judge LLM 本身有偏见**：如果 judge 用的模型和 agent LLM 同源，可能对同类型错误不敏感。建议 judge 用不同模型。
- **Golden examples 覆盖不全**：10-20 条不可能穷举所有场景，未被覆盖的 case 评分准确度会下降。需持续补充。
- **评分≠真实业务效果**：Golden 对齐不等于 ACOS 下降。最终需要 AB 测试验证 skill 变更对实际广告指标的影响。

## 5. 共识结论

- SkillOpt 是纯外挂，离线运行，不注入生产环境
- 工程接入成本低（~200 行代码），瓶颈是 scorer 设计
- 否决了 warnings 计数、覆盖率、采纳率等间接信号
- 采用 **LLM-as-Judge + Golden Examples** 方案：运营提供正确答案做锚点，judge LLM 对比评估
- 建议先用离线回放验证 judge 评分分布，确认信号可信再投入训练
