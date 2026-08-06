"""知识库加载器 — 把 docs/knowledge_base/ 按调用点切片注入到 system prompt。

核心原则：
1. KB 是业务规则唯一源（阈值/阶段映射/红线/关键词策略以 docs/knowledge_base 为准）
2. KB 文件只读；ENUM 翻译/preset 切片全部在加载层完成
3. 每次 LLM 调用按 PRESETS 拿最小切片，不打包全量

ENUM_MAP：以 layer_options.toml 为中文 term 唯一权威源；KB 英文 enum 单向翻译。
"""
import logging
import re
from functools import cached_property
from pathlib import Path

import yaml

from app.models.layers import CONFIG_ENUM_TO_ZH

logger = logging.getLogger(__name__)

# 编号标题切块：支持 "## 1. 标题" / "## 7A. 标题"，以及
# "### 3.1 标题"。H2 选中时包含其全部子节；H3 选中时只保留该子节。
_NUM_HEADING = re.compile(
    r"^(#{2,3})\s+(\d+(?:\.\d+)*(?:[A-Za-z])?)\.?(?:\s|$)", re.M,
)


def _split_sections(text: str) -> dict[str, str]:
    """按编号 H2/H3 标题把 KB 文件切成 {节号: 含标题全文}。

    H2 节号（如 ``3``）包含整个一级节；H3 节号（如 ``3.2``）只包含
    当前子节。无编号标题的文件（如 KB08）返回空 dict，``build`` 退回整文件。
    """
    marks = [
        (m.group(2), len(m.group(1)), m.start())
        for m in _NUM_HEADING.finditer(text)
    ]
    out: dict[str, str] = {}
    for i, (num, level, start) in enumerate(marks):
        if level == 2:
            end = next(
                (next_start for _, next_level, next_start in marks[i + 1:]
                 if next_level == 2),
                len(text),
            )
        else:
            end = marks[i + 1][2] if i + 1 < len(marks) else len(text)
        out[num] = text[start:end].strip()
    return out


class KnowledgeBase:
    VERSION = "v3.2.0"

    # parents[0]=app/llm, parents[1]=app, parents[2]=ad-direction-agent, parents[3]=repo root
    ROOT = Path(__file__).resolve().parents[3] / "docs" / "knowledge_base"

    # 调用点 → 文件 ID 列表（评审过的最小切片）
    PRESETS: dict[str, list[str]] = {
        # 策略层：广告目的 + 关键词类型推荐（purpose-agent）
        "purpose_tactics":     ["01", "02", "03", "04", "06", "09", "10", "14"],
        # 执行层：方向推荐。KB01 是元信息非业务规则，03 只留定位/阶段/目的阈值，
        # 22 只留 §0 核心原则（其余 §1-5 是活动级调整流程）。
        "execution_direction": ["02", "03:1,2,4", "04", "05", "09", "14", "22:0"],
        # P3：目标 ACOS + 预算/Bid 推荐（ASIN 级）。
        # 01/05/10/11 移除（01=元信息，05/10/11=活动级粒度错配）。
        # 补 25号 §1-3,5（ASIN 级目标ACOS/幅度/数量/日预算推荐）。
        "p3_recommend":        ["03:1,2,4,5", "09", "14", "25:1,2,3,5"],
        # 综合分析报告
        "analyze_report":      ["02", "05", "09", "12", "14"],
        # AI 聊天（demo）
        "chat":                ["02", "14"],
        # Campaign 活动调整 —— 2026-06-24 切片化 + 精准/广泛分流(治注意力稀释)。
        # 语法 "fid:节号" = 只注入该文件指定一级节;无冒号 = 整文件。
        # 共同剔除的稀释节(对单活动调整是噪声/越界/与 prompt 输出格式冲突):
        #   KB18 §2(13步开发流程,含Ontology校验/组合回算/ERP输出) §4(与KB17§1/KB19§1重复)
        #        §5(新增活动,属 new_campaign) §6/§7(ERP输出schema)
        #   KB17 §0(输入schema) §6(记忆修正,campaign 未接记忆信号) §7A(Ontology校验)
        #        §8(组合预算回算,属 budget_reallocation agent) §9(输出格式,与 prompt 冲突)
        #   KB15 §0(示例) §5(速查卡输出schema)；KB19 §10(汇总,与各节重复)
        #   KB22 §1(类型判定,与KB18§1重复) §4(输出字段) §5(90行YAML示例,与 prompt 冲突)
        # 精准/广泛差异:精准要广告位(KB15§3/KB19§5§9/KB22§2),广泛要否词(KB19§7§8/KB22§3)。
        # KB17 §3 细分为 3.1-3.4；build_campaign_adjustment() 会按当前广告方向
        # 收窄，直接 build(preset) 时保留全量，兼容已有调用和离线检查。
        # ── Campaign 活动调整切片（2026-07-29 重建）──
        # 共同原则：
        #   - 只注入 LLM 决策真正需要的知识；代码护栏已有的不重复注入（10号§6-7）
        #   - 与 prompt 输出格式冲突的节剔除（18号§5-7 / 17号§9 / 15号§0§5 / 22号§4-5）
        #   - 精准流核心：广告位(15§3/19§5§9/22§2) + 升降级(exact_grade)
        #   - 广泛流核心：否词+自动组(19§7-8/broad_auto) + KB31 搜索词提精规则；不含广告位
        "campaign_adjustment_exact": [
            "18:1,3", "17:1,2,3.1,3.2,3.3,3.4,4,5,7", "15:1,2,3,4",
            "19:1,2,3,4,5,6,9", "22:0,2", "21:0,1,2,3,4",
            "10:1,2", "03:7,8,10,12", "14:1,9,16,20,21",
            "30:0,1,2", "32",
        ],
        "campaign_adjustment_broad": [
            "18:1,3", "17:1,2,3.1,3.2,3.3,3.4,4,5,7", "15:1,2,4",
            "19:1,2,3,4,6,7,8", "22:0", "21:0,1,2,3,4",
            "10:1,2", "03:7,8,10,12", "14:1,9,16,20,21",
            # 投影层重切（P1-2）：
            # 30号 只注入业务语义（两套码原理/动作总表/paused_campaign），排除 3-7（复合展开/生命周期/ERP映射/幂等/未注册 = 代码层）
            "30:0,1,2",
            # 31号 只注入广泛侧决策要点（核心结论/探索强度/经营模式/四档分支/自动投放组/转精准后处理/停止投放/扩词），
            # 排除 1(与18/22重复) 5/6(19号有) 7(18号有) 9(ONT有) 10(23号有) 11(精准侧) 15(输出字段=代码管)
            "31:0,2,3,4,8,12,13,14",
        ],
        # Campaign 策略总览(执行总纲)
        "campaign_overview":    ["02", "04", "09"],
        # Campaign 新增活动: LLM 只判"选哪些词 + keyword_class"。
        #   清仓期禁扩词由上游开关控制(campaign_new_enabled)，不进 prompt。
        "new_campaign":         ["16:1,6", "06", "02:1,2,3,4,5,6", "28:2"],
        # Campaign 预算回算 agent: 只注入分配决策需要的切片。
        #   §5(字段语义: group_requested_delta / low_bid_retention_release)
        #   §7(组合预算分配优先级) §3.6(主力组保护) §4.1(核心原则)。
        #   算术由代码预聚合, LLM 只判分配方式+倾斜方向+解释。
        #   §4.2(活动预算和≠花费) §4.3(超配健康比) 补上下界概念。
        "budget_reallocation":  ["23:5,7,3.6,4.1,4.2,4.3"],
        # 核心词语义判定: KB29 §1-3(判定公式+冲突+语义) + KB28 §2(R1-R4定义)。
        # 不含 KB29 §4-6(数据规则/词池/来源=代码处理)、§7(输出=prompt定义)、§8(数量=代码处理)。
        "semantic_core":        ["29:1,2,3", "28:2"],
    }

    # KB 英文 enum → 项目中文 enum（从 app.models.layers.CONFIG_ENUM_TO_ZH 派生的子集）。
    # 翻译时长串优先 + 整词边界，避免 traffic 误匹配 traffic_opportunity
    # 权威映射在 app.models.layers.CONFIG_ENUM_TO_ZH；这里只筛选 KB 英文 token 子集，
    # 不复制中文值或 ERP code，避免双源漂移。
    ENUM_MAP: dict[str, str] = {
        f"{k}": v for k, v in CONFIG_ENUM_TO_ZH.items()
        if k.isascii() and k.islower()
    }

    _DIRECTION_ACTION_SECTIONS: dict[str, str] = {
        "push_natural_rank": "3.1",
        "推进自然位": "3.1",
        "expand_keywords": "3.2",
        "新增扩词": "3.2",
        "optimize_acos": "3.3",
        "优化ACOS": "3.3",
        "balance_maintain": "3.4",
        "平衡维持": "3.4",
    }

    # 文件 ID → 相对路径（含子目录）
    _FILE_PATHS: dict[str, str] = {
        "01": "01-知识库元信息.md",
        "02": "02-标签维度定义.md",
        "03": "03-阈值参数.md",
        "04": "04-触发规则.md",
        "05": "05-动作规则.md",
        "06": "06-关键词类型规则.md",
        "07": "07-广告位规则.md",
        "08": "08-竞品规则.md",
        "09": "09-冲突裁决规则.md",
        "10": "10-安全护栏.md",
        "11": "11-风险评分.md",
        "12": "12-输出规范.md",
        "13": "13-记忆系统规则.md",
        "14": "14-名词定义.md",
        "15": "执行规则/15-策略约束值规则.md",
        "16": "执行规则/16-新增活动规则.md",
        "17": "执行规则/17-问题诊断与动作优先级.md",
        "18": "执行规则/18-广告执行调整流程.md",
        "19": "执行规则/19-执行层数值规则.md",
        "21": "执行规则/21-淘汰广告活动规则.md",
        "22": "执行规则/22-调整广告活动规则.md",
        "23": "执行规则/23-广告组合与预算分配规则.md",
        "24": "执行规则/24-女装白牌专项.md",
        "25": "25-目标ACOS预算Bid推荐.md",
        "28": "执行规则/28-新增词来源相关性与场景化配额规则.md",
        "29": "执行规则/29-核心词定义规则.md",
        "30": "30-动作词表与映射.md",
        "31": "执行规则/31-广泛自动词组调整规则.md",
        "32": "执行规则/32-精准组合升降级规则.md",
    }

    def __init__(self):
        self._cache: dict[str, str] = {}
        self._sections: dict[str, dict[str, str]] = {}   # fid → {节号: 节全文}
        self._enum_patterns = self._compile_enum_patterns()
        self._preload()

    def _compile_enum_patterns(self) -> list[tuple[re.Pattern, str]]:
        """编译 ENUM_MAP 正则；长串优先，避免短串先匹配。"""
        compiled: list[tuple[re.Pattern, str]] = []
        for key in sorted(self.ENUM_MAP, key=len, reverse=True):
            pat = re.compile(rf"(?<![A-Za-z_]){re.escape(key)}(?![A-Za-z_])")
            compiled.append((pat, self.ENUM_MAP[key]))
        return compiled

    def _translate_enums(self, text: str) -> str:
        for pat, repl in self._enum_patterns:
            text = pat.sub(repl, text)
        return text

    def _preload(self) -> None:
        # 只加载 PRESETS 实际引用的文件，减少 IO。spec 形如 "18" 或 "18:1,3"，取冒号前为 fid
        needed: set[str] = set()
        for ids in self.PRESETS.values():
            for spec in ids:
                needed.add(spec.split(":", 1)[0])

        for fid in sorted(needed):
            rel = self._FILE_PATHS.get(fid)
            if rel is None:
                logger.warning("KB file id %s not in _FILE_PATHS, skipping", fid)
                continue
            path = self.ROOT / rel
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.error("KB file missing: %s", path)
                self._cache[fid] = ""
                continue
            translated = self._translate_enums(raw)
            self._cache[fid] = translated
            self._sections[fid] = _split_sections(translated)   # 预切节，build 按需取
            logger.info("KB loaded %s (%s): %d chars, %d 节", fid, rel,
                        len(translated), len(self._sections[fid]))

        # 打印每个 preset 拼接后字符数（便于上线观察 token 体量）
        for name in self.PRESETS:
            logger.info("KB preset %s: %d chars", name, len(self.build(name)))

    def build(self, preset: str) -> str:
        """按 preset 拼接 KB 内容。spec 支持 "fid"(整文件) 或 "fid:节号,节号"(切片注入)。"""
        ids = self.PRESETS.get(preset)
        if ids is None:
            raise KeyError(f"未知的 KB preset: {preset}")
        return self._build_specs(preset, ids)

    @cached_property
    def _ontology_contract(self) -> dict:
        """懒加载 runtime_contract.yaml。缺失/损坏时返回空契约 —— 不注入 Ontology Card，不阻断分析。"""
        path = self.ROOT / "ontology" / "runtime_contract.yaml"
        try:
            if not path.exists():
                raise FileNotFoundError(f"Ontology 运行时契约缺失: {path}")
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            logger.warning("Ontology 运行时契约加载失败 [%s]: %s（本次不注入 Ontology Card）", path, e)
            return {}

    def build_ontology_card(self, task_type: str) -> str:
        """从 runtime_contract.yaml 拼装精简 Ontology Card 注入 prompt。

        精准流不注入 ONT-RUNTIME-001（广泛/词组/自动禁止广告位）——
        精准广告允许广告位调整，这条规则对其是噪声。
        契约缺失/解析失败时返回空串：不注入、不阻断 AI 分析。
        """
        try:
            contract = self._ontology_contract
            if not contract:
                return ""
            injection = contract.get("ontology_card_injection") or {}
            common_ids = set(injection.get("always_inject") or [])
            if not common_ids:  # 兜底：契约未声明时注入除 001 外的全部
                common_ids = {
                    r["rule_id"] for r in contract.get("hard_rules", [])
                    if r.get("rule_id") != "ONT-RUNTIME-001"
                }
            if task_type != "exact":
                common_ids.update(injection.get("inject_unless_exact") or ["ONT-RUNTIME-001"])

            selected = [
                r for r in contract.get("hard_rules", [])
                if r.get("rule_id") in common_ids
            ]
            card = {
                "ontology_version": contract.get("version", ""),
                "execution_layers": contract.get("decision_grain", {}).get("execution_layers", {}),
                "mode_policies": contract.get("mode_policies", {}),
                "portfolio_groups": contract.get("portfolio_groups", {}),
                "portfolio_routing": contract.get("portfolio_routing", {}),
                "hard_rules": selected,
                "action_bundle": contract.get("action_bundle", {}),
            }
            return "【Ontology Runtime Contract】\n" + yaml.safe_dump(
                card, allow_unicode=True, sort_keys=False,
            ).strip()
        except Exception as e:
            logger.warning("Ontology Card 拼装失败 [%s]: %s（本次不注入 Ontology Card）", task_type, e)
            return ""

    def build_campaign_adjustment(
        self,
        task_type: str,
        ad_directions: list[str] | tuple[str, ...] | str | None = None,
    ) -> str:
        """按 Campaign 流和当前广告方向构建最小调整规则包。

        无方向、空方向或未知方向时回退为 KB17 的四个动作子节，避免缺少
        业务规则；已识别方向只注入对应的 3.x 动作矩阵。
        """
        preset = (
            "campaign_adjustment_exact"
            if task_type == "exact"
            else "campaign_adjustment_broad"
        )
        if isinstance(ad_directions, str):
            directions = [ad_directions]
        else:
            directions = list(ad_directions or [])

        selected = {
            self._DIRECTION_ACTION_SECTIONS.get(str(direction).strip())
            for direction in directions
            if str(direction).strip()
        }
        selected.discard(None)
        if not selected:
            selected = {"3.1", "3.2", "3.3", "3.4"}

        ids = list(self.PRESETS[preset])
        # 方向收窄：运营选了哪些方向就只注入对应的 17号§3 动作矩阵
        action_spec = "17:1,2," + ",".join(sorted(selected)) + ",4,5,7"
        for index, spec in enumerate(ids):
            if spec.startswith("17:"):
                ids[index] = action_spec
                break
        knowledge = self._build_specs(preset, ids)
        ontology_card = self.build_ontology_card(task_type)
        if ontology_card:
            return f"{knowledge}\n\n---\n\n{ontology_card}"
        return knowledge

    def _build_specs(self, preset: str, ids: list[str]) -> str:
        """按已解析的 spec 列表拼接内容，供静态和运行时 preset 复用。"""
        out: list[str] = []
        for spec in ids:
            fid, _, sel = spec.partition(":")
            if not sel:                                    # 整文件
                content = self._cache.get(fid, "")
            else:                                          # 切片：只取指定一级节
                secs = self._sections.get(fid, {})
                wanted = [s.strip() for s in sel.split(",") if s.strip()]
                missing = [w for w in wanted if w not in secs]
                if missing:   # 节号失配(KB 改版重编号/写错)→ 告警暴露，绝不静默丢规则
                    logger.warning("KB preset %s: KB%s 缺节 %s (现有节 %s)",
                                   preset, fid, missing, sorted(secs))
                content = "\n\n".join(secs[w] for w in wanted if w in secs)
            if content:
                out.append(content)
        return "\n\n---\n\n".join(out)


# 模块级单例 — 进程启动一次性预热
kb = KnowledgeBase()
