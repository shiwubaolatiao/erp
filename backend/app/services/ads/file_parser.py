"""
通用文件解析器
将 CSV / Excel / Word / PDF / HTML / JSON / 文本 等文件提取为纯文本，原样投喂给 LLM
不做列名识别、不做指标计算、不做摘要生成 —— AI 自己理解数据
"""
import io
import os
import re
import pandas as pd


def extract_text(file_bytes: bytes, filename: str, max_chars: int = 50000) -> str:
    """从任意文件中提取全部文本内容，原样返回

    支持: CSV / TSV / Excel / Word / PDF / HTML / JSON / XML / 纯文本
    返回: 文件的完整文本内容（截断到 max_chars）
    """
    ext = os.path.splitext(filename.lower())[1]

    extractors = {
        '.csv':  lambda: _csv(file_bytes, ','),
        '.tsv':  lambda: _csv(file_bytes, '\t'),
        '.xlsx': lambda: _excel(file_bytes),
        '.xls':  lambda: _excel(file_bytes),
        '.docx': lambda: _word(file_bytes),
        '.doc':  lambda: _word(file_bytes),
        '.pdf':  lambda: _pdf(file_bytes),
        '.html': lambda: _html(file_bytes),
        '.htm':  lambda: _html(file_bytes),
        '.json': lambda: _text(file_bytes),
        '.xml':  lambda: _text(file_bytes),
        '.txt':  lambda: _text(file_bytes),
        '.md':   lambda: _text(file_bytes),
        '.log':  lambda: _text(file_bytes),
    }

    extractor = extractors.get(ext, lambda: _text(file_bytes))
    try:
        text = extractor()
    except Exception as e:
        text = f"[解析失败: {filename} - {e}]"

    # 截断
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... (已截断，原文共 {len(text)} 字符)"
    return text


# ============================================================
# 各格式提取器 —— 只管提取，不做清洗
# ============================================================

def _csv(file_bytes: bytes, sep: str) -> str:
    """CSV / TSV -> DataFrame -> to_string"""
    for enc in ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'latin1']:
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), encoding=enc, sep=sep, on_bad_lines='skip')
            if len(df) > 0:
                return df.to_string(index=False)
        except Exception:
            continue
    return _text(file_bytes)


def _excel(file_bytes: bytes) -> str:
    """Excel -> 每个 Sheet 转文本"""
    xls = pd.ExcelFile(io.BytesIO(file_bytes))
    parts = []
    for sheet in xls.sheet_names:
        df = xls.parse(sheet)
        if len(df) > 0:
            parts.append(f"[Sheet: {sheet}]")
            parts.append(df.to_string(index=False))
            parts.append("")
    return "\n".join(parts) if parts else "[Excel 文件为空]"


def _word(file_bytes: bytes) -> str:
    """Word -> 段落 + 表格，全部转为文本"""
    from docx import Document
    doc = Document(io.BytesIO(file_bytes))
    parts = []

    # 正文段落
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    # 表格 -> 管道分隔文本
    for i, table in enumerate(doc.tables):
        parts.append(f"\n[表格 {i + 1}]")
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            parts.append(" | ".join(cells))

    return "\n".join(parts) if parts else "[Word 文档为空]"


def _pdf(file_bytes: bytes) -> str:
    """PDF -> 逐页提取文本 + 表格"""
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            # 页面文本
            text = page.extract_text()
            if text:
                parts.append(text)

            # 表格 -> 管道分隔
            tables = page.extract_tables()
            for t in tables:
                for row in t:
                    cells = [str(c or '').strip() for c in row]
                    parts.append(" | ".join(cells))

    return "\n".join(parts) if parts else "[PDF 未提取到文本，可能是扫描件]"


def _html(file_bytes: bytes) -> str:
    """HTML -> 去标签留文本"""
    text = _text(file_bytes)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _text(file_bytes: bytes) -> str:
    """纯文本：尝试多种编码直接解码"""
    for enc in ['utf-8', 'utf-8-sig', 'gbk', 'gb2312', 'latin1']:
        try:
            return file_bytes.decode(enc)
        except Exception:
            continue
    return "[文件无法解码]"
