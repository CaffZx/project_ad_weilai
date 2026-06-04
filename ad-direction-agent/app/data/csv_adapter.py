"""CSV/XLSX 数据源适配器

从本地文件读取ASIN数据:
- 单ASIN文件: {data_dir}/{asin}.csv 或 {asin}.xlsx
- 汇总文件: {data_dir}/asin_data.csv 或 asin_data.xlsx（包含所有ASIN）
"""

import logging
from pathlib import Path

from app.data.base import DataSourceAdapter
from app.data.field_mapping import (
    DEFAULT_FIELD_MAP,
    KEYWORD_PREFIX_RE,
    KEYWORD_FIELD_MAP,
    COMPETITOR_PREFIX_RE,
    COMPETITOR_FIELD_MAP,
    set_nested_value,
)
from app.models.asin_data import ASINData, KeywordData, CompetitorData

logger = logging.getLogger(__name__)


class CsvAdapter(DataSourceAdapter):
    """从CSV/XLSX文件读取ASIN数据的适配器"""

    def __init__(self, data_dir: str = "data/source", filename: str | None = None, key_column: str = "asin"):
        self.data_dir = Path(data_dir)
        self.filename = filename  # 自定义汇总文件名（如 asin_test_data.xlsx）
        self.key_column = key_column  # 汇总文件中的ASIN标识列名（如 "asin" 或 "父asin"）

    async def fetch_asin_data(self, asin: str,
                               meta_filter: list[str] | None = None,
                               days: int = 7) -> ASINData:
        """读取ASIN数据，优先单文件→汇总文件→返回缺失标记。days 仅用于接口兼容。"""
        # 1. 尝试单文件
        row = self._read_single_file(asin)
        if row:
            return self._row_to_asin_data(row, asin)

        # 2. 尝试汇总文件
        rows = self._read_consolidated_file()
        if rows and asin in rows:
            return self._row_to_asin_data(rows[asin], asin)

        # 3. 未找到
        return ASINData(
            asin=asin,
            data_missing=True,
            missing_fields=["asin_not_found"],
        )

    # ── 文件读取 ────────────────────────────────────────

    def _read_single_file(self, asin: str) -> dict | None:
        """读取 {asin}.csv 或 {asin}.xlsx，返回第一行数据"""
        for ext in (".csv", ".xlsx"):
            fp = self.data_dir / f"{asin}{ext}"
            if not fp.exists():
                continue
            try:
                if ext == ".csv":
                    return self._read_csv(fp)
                else:
                    rows = self._read_xlsx(fp)
                    return rows[0] if rows else None
            except Exception as e:
                logger.warning("读取文件失败 %s: %s", fp, e)
        return None

    def _read_consolidated_file(self) -> dict[str, dict] | None:
        """读取汇总文件，返回 {asin: row} 字典"""
        candidates: list[Path] = []
        if self.filename:
            fp = self.data_dir / self.filename
            if fp.suffix in (".csv", ".xlsx"):
                candidates.append(fp)
        for ext in (".csv", ".xlsx"):
            fp = self.data_dir / f"asin_data{ext}"
            if fp not in candidates:
                candidates.append(fp)

        for fp in candidates:
            if not fp.exists():
                continue
            try:
                is_csv = fp.suffix == ".csv"
                # 读取headers确定实际可用的key列
                raw = self._read_xlsx_raw(fp) if not is_csv else self._read_csv_raw(fp)
                if not raw:
                    continue
                headers, rows_iter = raw["headers"], raw["rows"]

                # 确定实际使用的key列
                actual_key = None
                for candidate_key in (self.key_column, "asin", "父asin", "parent_asin"):
                    if candidate_key in headers:
                        actual_key = candidate_key
                        break
                if not actual_key:
                    continue

                # 组装结果
                key_idx = headers.index(actual_key)
                result = {}
                for row in rows_iter:
                    if self._is_dup_header(row, headers):
                        continue
                    key = str(row[key_idx]).strip() if key_idx < len(row) and row[key_idx] else ""
                    if key:
                        result[key] = dict(zip(headers, [str(v) if v is not None else "" for v in row]))
                if result:
                    return result
            except Exception as e:
                logger.warning("读取汇总文件失败 %s: %s", fp, e)
        return None

    def _read_xlsx_raw(self, filepath: Path) -> dict | None:
        """读取xlsx的headers和rows迭代器（原始数据）"""
        try:
            from openpyxl import load_workbook
        except ImportError:
            return None
        wb = load_workbook(filepath, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h else "" for h in next(rows_iter, [])]
        # 需要将rows_iter具体化才能关闭workbook — 先全部读取
        all_rows = list(rows_iter)
        wb.close()
        return {"headers": headers, "rows": all_rows}

    def _read_csv_raw(self, filepath: Path) -> dict | None:
        """读取csv的headers和rows列表"""
        import csv
        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            all_rows = []
            for row in reader:
                # 将DictReader的OrderedDict转为普通dict
                all_rows.append({k: v for k, v in row.items()})
        return {"headers": list(headers), "rows": all_rows}

    @staticmethod
    def _is_dup_header(row_vals, hdrs: list[str]) -> bool:
        """检测行是否为重复表头（行值与列名重合）"""
        if not row_vals or not hdrs:
            return False
        if isinstance(row_vals, dict):
            vals = [str(row_vals.get(h, "")).strip() for h in hdrs]
        else:
            vals = [str(v).strip() if v else "" for v in row_vals]
        match_count = sum(1 for i, v in enumerate(vals) if i < len(hdrs) and v == hdrs[i])
        return match_count >= max(2, len(hdrs) * 0.4)

    def _read_csv(self, filepath: Path, keyed_by: str | None = "asin") -> dict:
        """读取CSV，可按列分组"""
        import csv

        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if keyed_by and reader.fieldnames and keyed_by in reader.fieldnames:
                result = {}
                for row in reader:
                    key = row.get(keyed_by, "").strip()
                    if key:
                        result[key] = row
                return result
            else:
                # 返回第一行（单文件场景）
                for row in reader:
                    return row  # type: ignore[return-value]
                return {}

    def _read_xlsx(self, filepath: Path, keyed_by: str | None = "asin") -> dict | list:
        """读取xlsx，按列分组"""
        try:
            from openpyxl import load_workbook
        except ImportError:
            logger.error("读取xlsx需要openpyxl: pip install openpyxl")
            return {}

        wb = load_workbook(filepath, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h else "" for h in next(rows_iter, [])]

        def _is_dup_header(row_vals: tuple, hdrs: list[str]) -> bool:
            """检测行是否为重复表头（行值与列名重合）"""
            if not row_vals or not hdrs:
                return False
            vals = [str(v).strip() if v else "" for v in row_vals]
            match_count = sum(1 for i, v in enumerate(vals) if i < len(hdrs) and v == hdrs[i])
            return match_count >= max(2, len(hdrs) * 0.4)

        if keyed_by and keyed_by in headers:
            key_idx = headers.index(keyed_by)
            result = {}
            for row in rows_iter:
                if _is_dup_header(row, headers):
                    continue
                key = str(row[key_idx]).strip() if row[key_idx] else ""
                if key:
                    result[key] = dict(zip(headers, [str(v) if v is not None else "" for v in row]))
            wb.close()
            return result
        else:
            for row in rows_iter:
                if _is_dup_header(row, headers):
                    continue
                wb.close()
                return dict(zip(headers, [str(v) if v is not None else "" for v in row]))
            wb.close()
            return {}

    # ── 数据映射 ────────────────────────────────────────

    def _row_to_asin_data(self, row: dict, fallback_asin: str) -> ASINData:
        """将CSV行数据映射为ASINData对象"""
        data = ASINData(asin=fallback_asin)

        # 收集关键词和竞品数据
        keyword_groups: dict[int, dict] = {}
        competitor_groups: dict[int, dict] = {}

        for col, value in row.items():
            col_clean = col.strip()
            raw_value = value.strip() if isinstance(value, str) else value

            # 关键词字段
            kw_match = KEYWORD_PREFIX_RE.match(col_clean)
            if kw_match:
                idx = int(kw_match.group(1))
                field = kw_match.group(2)
                if field in KEYWORD_FIELD_MAP:
                    keyword_groups.setdefault(idx, {})[KEYWORD_FIELD_MAP[field]] = raw_value
                continue

            # 竞品字段
            comp_match = COMPETITOR_PREFIX_RE.match(col_clean)
            if comp_match:
                idx = int(comp_match.group(1))
                field = comp_match.group(2)
                if field in COMPETITOR_FIELD_MAP:
                    competitor_groups.setdefault(idx, {})[COMPETITOR_FIELD_MAP[field]] = raw_value
                continue

            # 直接映射字段
            if col_clean in DEFAULT_FIELD_MAP:
                path = DEFAULT_FIELD_MAP[col_clean]
                set_nested_value(data, path, raw_value)

        # 添加关键词
        for idx in sorted(keyword_groups):
            kw = keyword_groups[idx]
            if "keyword" in kw:
                data.keywords.append(KeywordData(**kw))

        # 添加竞品
        for idx in sorted(competitor_groups):
            comp = competitor_groups[idx]
            if "asin" in comp:
                data.competitors.append(CompetitorData(**comp))

        # 更新keyword_count
        if not data.keyword_count and data.keywords:
            data.keyword_count = len(data.keywords)

        return data
