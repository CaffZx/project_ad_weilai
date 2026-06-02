# RAG 知识库落地执行方案

> **目标**: 将企业内部多种格式的知识文件(pdf/docx/md/xmind/...)转化为可被 Analyst Agent 语义检索的知识库
> **工期**: 3 天(Phase 1 基础管道) + 2 天(Phase 2 集成 Agent) + 持续(Phase 3 知识运营)

---

## 一、整体流程

```
┌─────────────────────────────────────────────────────────────┐
│  Phase 1: 知识工程管线(一次性建设)                             │
│                                                              │
│  原始文件                  处理后                             │
│  knowledge_raw/           kb/unstructured/    kb/vector_store/
│  ├── 泳装广告策略.pdf      → swimwear.md   →  chroma.sqlite3 │
│  ├── 竞品分析方法.docx     → competitor.md    (ChromaDB)     │
│  ├── ACOS优化.xmind        → acos_ops.md                    │
│  ├── 运营SOP_v3.pdf        → ops_sop.md                     │
│  └── ...                                                  │
│                                                              │
│  Step 1: 解析 → Step 2: 清洗 → Step 3: 分块 → Step 4: 嵌入   │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 2: 检索集成(Agent 侧)                                  │
│                                                              │
│  Analyst Observe 节点 → 构造检索查询 → ChromaDB.query()       │
│  → Top-3 相关文档片段 → 注入 Think System Prompt              │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Phase 3: 运营迭代(持续)                                      │
│                                                              │
│  知识更新 → 增量重建 → 检索效果评估 → 分块策略调整              │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、Phase 1: 知识工程管线(3 天)

### 2.1 依赖安装

```bash
pip install chromadb>=0.5.0          # 向量数据库
pip install pymupdf>=1.24.0          # PDF 解析(fitz, 中文支持好)
pip install python-docx>=1.1.0       # DOCX 解析
pip install xmindparser>=1.0.0       # XMind 解析
pip install langchain-text-splitters>=0.3.0  # 文本分块(只装这个子包)
pip install openpyxl>=3.1.0          # Excel 解析
pip install markdown>=3.6            # Markdown 转纯文本(去格式)
```

**为什么不用 LangChain 全家桶**：只用了它的 `RecursiveCharacterTextSplitter`(分块算法成熟)，不引入 DocumentLoader/VectorStore 等抽象。管道自己写，保持对流程的完全控制。

### 2.2 目录结构

```
ad-direction-agent/
├── knowledge_raw/                    # ★ 新建: 原始知识文件(不入 git)
│   ├── pdf/
│   │   ├── 泳装广告投放策略_v3.pdf
│   │   ├── 季节性调整指南.pdf
│   │   └── ...
│   ├── docx/
│   │   ├── 竞品分析方法论.docx
│   │   └── ...
│   ├── xmind/
│   │   ├── ACOS优化决策树.xmind
│   │   └── ...
│   └── md/
│       ├── 运营SOP.md
│       └── ...
│
├── kb/
│   ├── unstructured/                 # ★ 中间产物: 清洗后的 Markdown
│   │   ├── category_knowledge/
│   │   │   ├── swimwear.md          # 从 pdf 解析+清洗+人工校对
│   │   │   ├── lingerie.md
│   │   │   └── activewear.md
│   │   ├── operations/
│   │   │   ├── acos_optimization.md # 从 xmind 解析+结构化
│   │   │   ├── bidding_strategy.md
│   │   │   └── sop_daily_ops.md     # 从 pdf 解析
│   │   ├── competitor_analysis.md   # 从 docx 解析
│   │   └── general_best_practices.md
│   │
│   ├── structured/                   # 保留: YAML 结构化参数
│   │   └── ...
│   │
│   ├── vector_store/                 # ChromaDB 持久化目录
│   │   └── chroma.sqlite3
│   │
│   └── build_pipeline.py            # ★ 核心: 一键构建脚本
```

### 2.3 解析器实现

一个解析器对应一种文件格式，输出统一的 `ParsedDocument`：

```python
# kb/build_pipeline.py

from dataclasses import dataclass, field

@dataclass
class ParsedDocument:
    """解析后的统一结构"""
    source_path: str              # 原始文件路径
    title: str                    # 文档标题(从文件名或内容提取)
    sections: list[dict]          # [{heading, content, page/para_num}]
    metadata: dict                # {format, category, tags, version, author, date}

# ── PDF 解析器 ──
def parse_pdf(filepath: str) -> ParsedDocument:
    """PyMuPDF 逐页提取文本，按空行和标题模式分节"""
    import fitz
    doc = fitz.open(filepath)
    full_text = ""
    for page in doc:
        full_text += page.get_text() + "\n"

    # 清洗 PDF 常见噪音: 页眉页脚、页码、多余空行
    full_text = clean_pdf_noise(full_text)

    # 按标题模式分节(匹配 "一、"/"1."/"第X章" 等中文标题)
    sections = split_by_heading_patterns(full_text)

    title = extract_title(filepath, sections)
    return ParsedDocument(
        source_path=filepath,
        title=title,
        sections=sections,
        metadata={"format": "pdf", "pages": len(doc)}
    )

# ── DOCX 解析器 ──
def parse_docx(filepath: str) -> ParsedDocument:
    """python-docx 逐段落提取，按 Heading 样式分节"""
    from docx import Document
    doc = Document(filepath)
    sections = []
    current_heading = ""
    current_content = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        if para.style.name.startswith("Heading"):
            if current_content:
                sections.append({"heading": current_heading, "content": "\n".join(current_content)})
            current_heading = text
            current_content = []
        else:
            current_content.append(text)

    if current_content:
        sections.append({"heading": current_heading, "content": "\n".join(current_content)})

    return ParsedDocument(source_path=filepath, title=filepath.stem, sections=sections, metadata={"format": "docx"})

# ── XMind 解析器 ──
def parse_xmind(filepath: str) -> ParsedDocument:
    """xmindparser 提取思维导图节点树 → 缩进文本 → 按一级主题分节"""
    import xmindparser
    content = xmindparser.xmind_to_dict(filepath)

    def extract_tree(node, depth=0):
        """递归提取节点文本, 保留层级关系"""
        lines = [f"{'#' * (depth + 2)} {node['title']}"]
        for child in node.get("topics", []):
            lines.extend(extract_tree(child, depth + 1))
        return lines

    # XMind 的根→一级主题→二级主题→... 转为 Markdown 层级标题
    root = content[0]["topic"]
    sections = []
    for child in root.get("topics", []):
        section_lines = extract_tree(child)
        sections.append({"heading": child["title"], "content": "\n".join(section_lines)})

    return ParsedDocument(source_path=filepath, title=root["title"], sections=sections, metadata={"format": "xmind"})

# ── Markdown 解析器 ──
def parse_md(filepath: str) -> ParsedDocument:
    """原生解析, 按 ## 标题分节"""
    text = filepath.read_text(encoding="utf-8")
    sections = split_by_md_headers(text)
    title = sections[0]["heading"] if sections else filepath.stem
    return ParsedDocument(source_path=filepath, title=title, sections=sections, metadata={"format": "md"})
```

### 2.4 统一的解析入口

```python
# kb/build_pipeline.py (续)

PARSERS = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".doc": parse_docx,
    ".xmind": parse_xmind,
    ".md": parse_md,
    ".txt": parse_txt,
    ".xlsx": parse_xlsx,    # 每行转一条知识条目
    ".csv": parse_csv,
}

def parse_all_raw_files(raw_dir: str, output_dir: str) -> list[ParsedDocument]:
    """遍历 knowledge_raw/ 所有文件 → 解析 → 存为 Markdown 中间产物"""
    raw_path = Path(raw_dir)
    output_path = Path(output_dir)

    all_docs = []
    for ext, parser in PARSERS.items():
        for filepath in raw_path.rglob(f"*{ext}"):
            print(f"解析: {filepath}")
            try:
                doc = parser(filepath)
                all_docs.append(doc)

                # 写入中间产物: Markdown 格式, 人工可校对
                output_file = output_path / filepath.relative_to(raw_path).with_suffix(".md")
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(doc.to_markdown(), encoding="utf-8")

            except Exception as e:
                print(f"  ⚠ 失败: {e}")

    return all_docs
```

### 2.5 文本清洗

```python
# kb/text_cleaner.py

def clean_pdf_noise(text: str) -> str:
    """去掉 PDF 常见噪音"""
    import re
    # 去页眉页脚(重复行)
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 去纯数字页码
        if re.match(r'^\d{1,4}$', line):
            continue
        # 去 "第X页 / 共Y页"
        if re.match(r'^第\d+页', line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)

def clean_xmind_noise(text: str) -> str:
    """XMind 解析后去掉连接线、图标等无意义文本"""
    import re
    # xmindparser 可能残留的连接线字符
    text = re.sub(r'[-─═]{3,}', '', text)
    return text

def validate_chunk(chunk: str) -> bool:
    """检查分块是否有效"""
    if len(chunk) < 50:           # 太短(只有标题), 合并到相邻块
        return False
    if len(chunk) > 2000:         # 太长(超过 embedding 窗口), 再分
        return False
    return True
```

### 2.6 分块策略

```python
# kb/chunker.py

from langchain_text_splitters import RecursiveCharacterTextSplitter

def chunk_document(doc: ParsedDocument) -> list[dict]:
    """
    分块策略:
    1. Markdown: 按 ## 标题自然分(每个 section 就是一个 chunk)
    2. PDF/DOCX: 用 RecursiveCharacterTextSplitter 按语义边界分
    3. XMind: 每个一级主题 = 一个 chunk, 保留子树结构

    每个 chunk 最终:
    - 800-1500 字符(匹配 DeepSeek embedding 最佳窗口)
    - 包含标题路径(上下文): "泳装广告策略 > ACOS优化 > 否定词策略"
    - 带 metadata: source, category, tags, title
    """
    chunks = []

    for section in doc.sections:
        heading = section.get("heading", "")
        content = section.get("content", "")
        full_text = f"# {heading}\n\n{content}"

        if len(full_text) <= 1500:
            # 直接作为一个 chunk
            chunks.append({
                "text": full_text,
                "title_path": build_title_path(doc, heading),
                "metadata": {
                    **doc.metadata,
                    "heading": heading,
                    "char_count": len(full_text),
                }
            })
        else:
            # 长节: 递归分块
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=1200,
                chunk_overlap=150,         # 重叠 150 字符, 防止断语义
                separators=["\n\n", "\n", "。", ".", "，", " ", ""]  # 优先在自然边界断
            )
            sub_chunks = splitter.split_text(full_text)
            for i, sub in enumerate(sub_chunks):
                if not validate_chunk(sub):
                    continue
                chunks.append({
                    "text": sub,
                    "title_path": build_title_path(doc, f"{heading}({i+1})"),
                    "metadata": {
                        **doc.metadata,
                        "heading": heading,
                        "chunk_index": i,
                        "char_count": len(sub),
                    }
                })

    return chunks

def build_title_path(doc: ParsedDocument, heading: str) -> str:
    """构建标题面包屑: '文档标题 > 一级标题 > 二级标题'"""
    return f"{doc.title} > {heading}"
```

### 2.7 嵌入和入库

```python
# kb/embedder.py

import chromadb
from chromadb.config import Settings

def build_vector_store(docs_dir: str, store_dir: str):
    """
    从 kb/unstructured/ 的所有 .md 文件 → 分块 → 嵌入 → ChromaDB

    一次运行，全量重建。适合知识库文件数量 < 100 的情况。
    """
    # 1. 遍历 Markdown 中间产物
    import glob
    md_files = list(Path(docs_dir).rglob("*.md"))

    # 2. 解析 + 分块
    all_chunks = []
    for md_file in md_files:
        doc = parse_md(md_file)
        chunks = chunk_document(doc)

        # 注入分类和标签(从文件路径推导)
        category = infer_category(md_file)      # "swimwear" / "operations" / "general"
        tags = infer_tags(doc)                   # ["acos", "bidding", "keywords"]

        for chunk in chunks:
            chunk["metadata"]["category"] = category
            chunk["metadata"]["tags"] = ",".join(tags)
            all_chunks.append(chunk)

    print(f"共 {len(all_chunks)} 个分块待嵌入")

    # 3. 初始化 ChromaDB
    chroma_client = chromadb.PersistentClient(path=store_dir)
    collection = chroma_client.get_or_create_collection(
        name="ad_knowledge",
        metadata={"hnsw:space": "cosine"}  # 余弦相似度
    )

    # 4. 批量嵌入
    batch_size = 20
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i + batch_size]

        texts = [c["text"] for c in batch]
        metadatas = [c["metadata"] for c in batch]
        ids = [f"{c['metadata']['heading']}:{i+j}" for j, c in enumerate(batch)]

        # 调 DeepSeek Embedding API
        embeddings = deepseek_embed_batch(texts)

        collection.add(
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )
        print(f"  嵌入 {i+len(batch)}/{len(all_chunks)}")

    print(f"完成: {len(all_chunks)} 个分块已入库")

def deepseek_embed_batch(texts: list[str]) -> list[list[float]]:
    """调 DeepSeek Embedding API, 批量嵌入"""
    import httpx
    response = httpx.post(
        "https://api.deepseek.com/v1/embeddings",
        headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
        json={"model": "deepseek-embedding", "input": texts}
    )
    return [item["embedding"] for item in response.json()["data"]]
```

### 2.8 一键构建脚本

```python
# kb/build_pipeline.py  — 唯一入口

def main():
    import argparse
    parser = argparse.ArgumentParser(description="构建 RAG 知识库")
    parser.add_argument("--full", action="store_true", help="从 knowledge_raw/ 全量重建")
    parser.add_argument("--incremental", action="store_true", help="仅处理 knowledge_raw/ 的新增/变更文件")
    parser.add_argument("--rebuild-index", action="store_true", help="仅重建向量索引(不改 Markdown)")
    parser.add_argument("--dry-run", action="store_true", help="只列出待处理文件, 不实际执行")
    args = parser.parse_args()

    if args.full or args.incremental:
        # Step 1-2: 解析原始文件 → Markdown 中间产物
        docs = parse_all_raw_files("knowledge_raw", "kb/unstructured")
        print(f"解析完成: {len(docs)} 个文档")

    if args.full or args.rebuild_index:
        # Step 3-4: 分块 → 嵌入 → ChromaDB
        build_vector_store("kb/unstructured", "kb/vector_store")

if __name__ == "__main__":
    main()
```

---

## 三、Phase 2: 检索集成(2 天)

### 3.1 检索器实现

```python
# kb/retriever.py

class KnowledgeRetriever:
    """Analyst Agent 调用的检索接口"""

    def __init__(self, store_dir: str = "kb/vector_store"):
        self.client = chromadb.PersistentClient(path=store_dir)
        self.collection = self.client.get_collection("ad_knowledge")

    def retrieve(self, query: str, n_results: int = 3, filter_category: str = None) -> list[dict]:
        """
        语义检索 Top-N 相关文档片段

        query: 自然语言查询, 由 Agent 构造
        filter_category: 可选, 只检索特定品类的知识
        """
        where_filter = None
        if filter_category:
            where_filter = {"category": filter_category}

        results = self.collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where_filter,
        )

        formatted = []
        for i in range(len(results["documents"][0])):
            formatted.append({
                "content": results["documents"][0][i],
                "source": results["metadatas"][0][i].get("source_file", "unknown"),
                "title": results["metadatas"][0][i].get("title_path", ""),
                "category": results["metadatas"][0][i].get("category", "general"),
                "relevance": 1.0 - results["distances"][0][i] if results["distances"] else 1.0,
            })
        return formatted

    def format_for_prompt(self, docs: list[dict]) -> str:
        """将检索结果格式化为可注入 LLM prompt 的文本"""
        parts = []
        for i, doc in enumerate(docs, 1):
            parts.append(f"[参考知识 {i}] 来源: {doc['title']}")
            parts.append(doc["content"])
            parts.append("")
        return "\n".join(parts)
```

### 3.2 集成到 Analyst Agent

在 Analyst 的 **Observe 节点**(Think 之前)调用检索：

```python
# agents/nodes/analyst_node.py — observe 函数

async def observe_node(state: AnalystState) -> dict:
    """数据变换 + RAG 检索 + 异常标记"""
    data = state["asin_data"]
    kb_params = state["kb_params"]
    scenario = state["scenario"]

    # 构造检索查询(业务参数 → 自然语言)
    rag_query = build_rag_query(
        scenario=scenario["id"],
        product_stage=kb_params.get("product_stage"),
        ad_purposes=",".join(kb_params.get("ad_purposes", [])),
        acos=data.get("acos"),
        cvr=data.get("cvr"),
    )
    # → 例: "泳装 收割利润期 ACOS 35% 盈利型 如何优化ACOS 否定词策略"

    # 检索
    retriever = KnowledgeRetriever()
    category = infer_category_from_tags(kb_params)  # 泳装? 内衣?
    docs = retriever.retrieve(rag_query, n_results=3, filter_category=category)
    rag_context = retriever.format_for_prompt(docs)

    # 异常信号
    anomalies = detect_anomalies(data, kb_params, scenario)

    return {
        "rag_context": rag_context,
        "anomalies": anomalies,
    }

def build_rag_query(**kwargs) -> str:
    """将结构化参数转换为自然语言检索查询"""
    parts = []
    if kwargs.get("product_stage"):
        parts.append(f"产品阶段{kwargs['product_stage']}")
    if kwargs.get("ad_purposes"):
        parts.append(f"广告目的{kwargs['ad_purposes']}")
    if kwargs.get("acos") is not None and kwargs["acos"] > 40:
        parts.append(f"ACOS偏高{kwargs['acos']:.0f}% 如何降低")
    if kwargs.get("cvr") is not None and kwargs["cvr"] < 5:
        parts.append("转化率偏低")
    parts.append("广告优化策略")
    return " ".join(parts)
```

### 3.3 RAG 上下文注入 Think Prompt

```python
# agents/nodes/analyst_node.py — think 的 System Prompt

THINK_SYSTEM_PROMPT = """你是一个亚马逊广告运营分析师。

## 参考知识(来自企业知识库, 优先参考)
{rag_context}

## 分析任务
{analysis_task}

## 数据
{data_summary}

请基于参考知识和数据进行分析, 引用知识库中的策略时注明来源。
"""

# think_node 内:
system_prompt = THINK_SYSTEM_PROMPT.format(
    rag_context=state["rag_context"] or "无相关参考知识",
    analysis_task=build_analysis_task(state),
    data_summary=format_data_summary(state["asin_data"]),
)
messages = [SystemMessage(content=system_prompt)] + state["messages"]
response = await llm_with_tools.ainvoke(messages)
```

---

## 四、Phase 3: 知识运营(持续)

### 4.1 知识文件维护工作流

```
运营/产品编写新知识文档
        │
        ▼
放入 knowledge_raw/ 对应子目录
        │
        ▼
运行 python kb/build_pipeline.py --incremental
        │
        ├── 解析 pdf/docx/xmind → kb/unstructured/*.md
        │
        ├── 人工校对 Markdown 中间产物(可选但建议)
        │
        └── 增量更新 ChromaDB 向量索引
                │
                ▼
        Analyst Think 节点自动检索到新知识
```

### 4.2 历史案例自动入库

每周从 `analysis_history` 表提取高质量分析结果，自动生成案例知识：

```python
# kb/case_extractor.py (每周定时跑一次)

def extract_cases_from_history():
    """从 analysis_history 提取优质案例 → 写入 kb/unstructured/historical_cases/"""
    db = get_connection()

    # 选高质量案例: confidence=high, human_decision=approved, 非错误
    rows = db.execute("""
        SELECT parent_asin, acos, cvr, scenario, priority_directions,
               proposals, analysis_text, margin, product_stage
        FROM analysis_history
        WHERE confidence = 'high'
          AND human_decision = 'approved'
          AND error IS NULL
          AND created_at > datetime('now', '-30 days')
        ORDER BY created_at DESC
        LIMIT 20
    """).fetchall()

    # 生成案例 Markdown
    case_doc = "# 历史分析案例(自动生成)\n\n"
    for row in rows:
        case_doc += f"""## 案例: {row['parent_asin']} | {row['scenario']} | ACOS {row['acos']:.0f}%

- **产品阶段**: {row['product_stage']}
- **场景**: {row['scenario']}
- **广告方向**: {row['priority_directions']}
- **建议数量**: {len(json.loads(row['proposals']))}条

### 分析摘要
{row['analysis_text'][:500]}

---
"""

    # 写入
    out_path = Path("kb/unstructured/historical_cases/cases_auto.md")
    out_path.write_text(case_doc, encoding="utf-8")

    # 增量重建向量索引
    subprocess.run(["python", "kb/build_pipeline.py", "--rebuild-index"])
```

### 4.3 检索效果评估

```python
# kb/evaluate.py (按需运行)

def evaluate_retrieval():
    """用一组测试问题验证检索质量"""
    retriever = KnowledgeRetriever()

    test_queries = [
        ("ACOS 45% 泳装 如何降低", "acos_optimization"),
        ("旺季前应该怎么调整出价", "bidding_strategy"),
        ("零转化词怎么处理", "negate_keyword_rules"),
        ("竞品降价了怎么办", "competitor_analysis"),
    ]

    for query, expected_source in test_queries:
        results = retriever.retrieve(query, n_results=3)
        sources = [r["title"] for r in results]
        hit = any(expected_source in s for s in sources)
        status = "✓" if hit else "✗"
        print(f"{status} '{query}' → {sources}")
```

---

## 五、文件格式支持清单

| 格式 | 解析库 | 解析方式 | 注意事项 |
|------|--------|---------|---------|
| `.md` | 原生 Python | 按 `##` 标题分节 | 最理想的知识格式 |
| `.pdf` | PyMuPDF (fitz) | 逐页提取文本 + 清洗 | 扫描版 PDF(图片)不支持, 需 OCR |
| `.docx` | python-docx | 按 Heading 样式分节 | `.doc` 不支持(旧格式), 需先转为 docx |
| `.xmind` | xmindparser | 递归提取节点树 → MD 层级标题 | 仅支持 XMind 8 格式(.xmind), Zen 格式不支持 |
| `.txt` | 原生 Python | 按段落分 | 无结构信息, 分块质量依赖文本本身 |
| `.xlsx` | openpyxl | 每行一条知识条目 | 仅限表格型知识(如关键词对照表) |
| `.csv` | 原生 csv | 每行一条知识条目 | 同上 |

**不支持(当前阶段)**: 扫描 PDF(需 OCR)、PPT(需 python-pptx)、图片(需 OCR+VLM)、视频/音频。

---

## 六、待办清单

### Phase 1(3 天)
- [ ] 建 `knowledge_raw/` 目录, 收集各部门知识文件
- [ ] 安装依赖: chromadb, pymupdf, python-docx, xmindparser
- [ ] 实现 6 个解析器(`parse_pdf/docx/xmind/md/txt/xlsx`)
- [ ] 实现文本清洗(`clean_pdf_noise`, `clean_xmind_noise`)
- [ ] 实现分块器(`chunk_document` + `RecursiveCharacterTextSplitter`)
- [ ] 实现嵌入器(`build_vector_store` + DeepSeek Embedding API)
- [ ] 调通 `python kb/build_pipeline.py --full` 首次全量构建
- [ ] 人工校对 `kb/unstructured/*.md` 中间产物(运营团队配合)

### Phase 2(2 天)
- [ ] 实现 `KnowledgeRetriever`(检索 + 格式化)
- [ ] `observe_node` 加 `build_rag_query` + 检索调用
- [ ] `think_node` 的 System Prompt 加 `{rag_context}` 占位
- [ ] 联调: 有 RAG vs 无 RAG 对比测试(同一 ASIN, 两次分析)
- [ ] 验证: RAG 知识被正确引用在最终 analysis_text 中

### Phase 3(持续)
- [ ] 知识文件更新 → `--incremental` 增量构建
- [ ] 历史案例自动提取脚本(`case_extractor.py`) + cron 调度
- [ ] 检索效果评估(`evaluate.py`) + 分块策略优化
- [ ] ChromaDB 迁移评估: 知识量 >10K chunks 后是否需要 Milvus/PGVector
