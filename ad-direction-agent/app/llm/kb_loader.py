"""知识库加载器 — 把 docs/knowledge_base/ 按调用点切片注入到 system prompt。

核心原则：
1. KB 是业务规则唯一源（阈值/阶段映射/红线/关键词策略以 docs/knowledge_base 为准）
2. KB 文件只读；ENUM 翻译/preset 切片全部在加载层完成
3. 每次 LLM 调用按 PRESETS 拿最小切片，不打包全量

ENUM_MAP：以 layer_options.toml 为中文 term 唯一权威源；KB 英文 enum 单向翻译。
"""
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 一级标题切块：匹配 "## 1. 标题" / "## 7A. 标题"，捕获节号(1 / 7A)
_H2_NUM = re.compile(r"^##\s+(\d+[A-Za-z]?)\.", re.M)


def _split_sections(text: str) -> dict[str, str]:
    """按 ## 一级编号标题把 KB 文件切成 {节号: 含标题全文(到下一节前)}。

    仅切「## N.」编号标题(KB18/17/15/19/22/16 等)；无编号标题的文件
    (如 KB08) 切不出节号 → 返回空 dict,build 退回整文件。
    """
    marks = [(m.group(1), m.start()) for m in _H2_NUM.finditer(text)]
    out: dict[str, str] = {}
    for i, (num, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(text)
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
        # 执行层：方向推荐（去掉 18，22 §1 已覆盖类型判定；§2 11 步流程是开发者编排指南）
        "execution_direction": ["01", "02", "03", "04", "05", "09", "14", "22"],
        # P3：目标 ACOS + 预算/Bid 推荐（ASIN 级）。
        # 2026-06-18 修复4：移除 19（KB19 是活动级数值矩阵：预算+20%/-10% 等，对 ASIN 级日预算/目标ACOS
        # 是粒度错配、且"ACOS上限/目标"易引发幻觉）。ASIN 级上限/预算锚由 KB03 提供。
        "p3_recommend":        ["01", "03", "05", "09", "10", "11", "14"],
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
        "campaign_adjustment_exact": [
            "18:1,3", "17:1,2,3,4,5,7", "15:1,2,3,4", "19:1,2,3,4,5,6,9", "22:0,2", "21",
        ],
        "campaign_adjustment_broad": [
            "18:1,3", "17:1,2,3,4,5,7", "15:1,2,4", "19:1,2,3,4,6,7,8", "22:0,3", "21",
        ],
        # Campaign 策略总览(执行总纲)：维度/广告目的→方向倾向 + 目的触发 + 取舍优先级
        # 仅做定性指挥(不写数值),故不引入 15(数值规则);保持小切片。
        "campaign_overview":    ["02", "04", "09"],
        # Campaign 新增活动: LLM 只判"选哪些词 + keyword_class + 文本",不算任何数值。
        #   2026-06-24 切片:KB16 只留 §1(触发场景) §6(阻断);剔除 §2(预算)§3(bid)§4(广告位)
        #   §5(输出schema)——这些是【代码】按 KB16 定的数值,prompt 明令 LLM 禁止输出 budget/
        #   bid/campaign_name/placement,§5 还与 prompt 的 JSON 输出格式直接冲突(诱导越界)。
        #   KB02 只留 §1-6(策略语境),剔除 §7-10(特殊场景/广告位/预算组合/竞品态势,与建词无关)。
        #   KB06 全留(keyword_class 判定核心);KB08 全留(竞品词姿态,无编号标题不切片)。
        "new_campaign":         ["16:1,6", "06", "02:1,2,3,4,5,6", "08"],
        # Campaign 预算回算 agent: KB23 自包含三层回算算法(§5 增量/§6 二次分配/§7 分配方式
        #   /§3.1A 组内优先级/§9 输出/§10 护栏)。算术由代码预聚合,LLM 只判分配方式+组内排序+解释。
        "budget_reallocation":  ["23"],
    }

    # KB 英文 enum → 项目中文 enum（与 layer_options.toml 对齐）
    # 翻译时长串优先 + 整词边界，避免 traffic 误匹配 traffic_opportunity
    ENUM_MAP: dict[str, str] = {
        # product_stage
        "testing": "测试期",
        "pushing": "推进期",
        "harvesting": "收割利润期",
        "maintaining": "维持期",
        "liquidating": "清货期",            # KB 有，config 暂无，保留中文化兜底
        # ad_purpose
        "traffic": "引流型",
        "conversion": "转化型",
        "ranking": "排名型",
        "profit": "盈利型",
        # 注意：clearance/清货型 不是广告目的，也不是核心策略标签。
        # 广告目的由 ad-purpose-agent 权威产出，只有 4 个：traffic/conversion/ranking/profit。
        # 清货是场景(scenario=clearance「清仓止损」)与产品阶段(liquidating/清货期)。
        # 已从 KB 12-输出规范.md 枚举中移除 Clearance(2026-06-05)；此处不翻译，双重防止它混入广告目的语境。
        # season（KB 英文 → config 中文）
        "off_season": "淡季",
        "peak_preparation": "旺季准备",
        "pre_peak": "旺季准备",
        "peak": "大旺季",
        "post_peak": "旺季末期",
        # ad_direction
        "push_natural_rank": "推进自然位",
        "expand_keywords": "新增扩词",
        "optimize_acos": "优化ACOS",
        "balance_maintain": "平衡维持",
        # target_keyword_strategy
        "broad": "大词",
        "long_tail": "长尾词",
        "long-tail": "长尾词",
        "competitor": "竞品词",
        "brand": "品牌词",
        "custom": "自定义",
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
        # # 24-30 预留槽位（文件暂未创建，启动时 logger.error 提示缺失，缓存空串不崩）
        # "24": "执行规则/24-预留.md",
        # "25": "执行规则/25-预留.md",
        # "26": "执行规则/26-预留.md",
        # "27": "执行规则/27-预留.md",
        # "28": "执行规则/28-预留.md",
        # "29": "执行规则/29-预留.md",
        # "30": "执行规则/30-预留.md",
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
