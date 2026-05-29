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


class KnowledgeBase:
    VERSION = "v3.1.0"

    # parents[0]=app/llm, parents[1]=app, parents[2]=ad-direction-agent, parents[3]=repo root
    ROOT = Path(__file__).resolve().parents[3] / "docs" / "knowledge_base"

    # 调用点 → 文件 ID 列表（评审过的最小切片）
    PRESETS: dict[str, list[str]] = {
        # 策略层：广告目的 + 关键词类型推荐（purpose-agent）
        "purpose_tactics":     ["01", "02", "03", "04", "06", "09", "10", "14"],
        # 执行层：方向推荐（去掉 18，22 §1 已覆盖类型判定；§2 11 步流程是开发者编排指南）
        "execution_direction": ["01", "02", "03", "04", "05", "09", "14", "22"],
        # P3：目标 ACOS + 预算/Bid 推荐
        "p3_recommend":        ["01", "03", "05", "09", "10", "11", "14", "19"],
        # 综合分析报告
        "analyze_report":      ["02", "05", "09", "12", "14"],
        # AI 聊天（demo）
        "chat":                ["02", "14"],
        # Campaign 活动调整
        "campaign_adjustment":  ["18", "19", "21", "22"],
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
        # 注意：clearance/清货型 不是广告目的（清货是产品阶段 liquidating/清货期）。
        # 不在此翻译，避免在广告目的语境中引入"清货型"。
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
        # keyword_type
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
        "13": "13-记忆结构.md",
        "14": "14-名词定义.md",
        "16": "执行规则/16-新增活动规则.md",
        "18": "执行规则/18-广告执行调整流程.md",
        "19": "执行规则/19-执行层数值规则.md",
        "21": "执行规则/21-淘汰广告活动规则.md",
        "22": "执行规则/22-调整广告活动规则.md",
    }

    def __init__(self):
        self._cache: dict[str, str] = {}
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
        # 只加载 PRESETS 实际引用的文件，减少 IO
        needed: set[str] = set()
        for ids in self.PRESETS.values():
            needed.update(ids)

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
            logger.info("KB loaded %s (%s): %d chars", fid, rel, len(translated))

        # 打印每个 preset 拼接后字符数（便于上线观察 token 体量）
        for name in self.PRESETS:
            logger.info("KB preset %s: %d chars", name, len(self.build(name)))

    def build(self, preset: str) -> str:
        """按 preset 拼接对应文件内容，用 '---' 分隔。"""
        ids = self.PRESETS.get(preset)
        if ids is None:
            raise KeyError(f"未知的 KB preset: {preset}")
        sections: list[str] = []
        for fid in ids:
            content = self._cache.get(fid, "")
            if content:
                sections.append(content)
        return "\n\n---\n\n".join(sections)


# 模块级单例 — 进程启动一次性预热
kb = KnowledgeBase()
