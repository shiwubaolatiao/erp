"""注释覆盖率审计 — 找出注释/文档严重不足的源文件

用法: python tools/audit_comments.py [--min-lines 20] [--threshold 0.10]
判定标准：
  - 注释行（# 开头）+ 文档字符串行 占总行数比例
  - 有 def/class 但缺 docstring 的数量
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # backend/


def audit_py(path: Path):
    text = path.read_text(encoding='utf-8')
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return None
    comment_lines = 0
    in_doc = False
    doc_lines = 0
    for l in lines:
        s = l.strip()
        if s.startswith('#'):
            comment_lines += 1
        # 粗略统计三引号块
        n3 = s.count('"""') + s.count("'''")
        if n3:
            if in_doc:
                doc_lines += 1
                if n3 % 2 == 1:
                    in_doc = False
            else:
                doc_lines += 1
                if n3 % 2 == 1:
                    in_doc = True
        elif in_doc:
            doc_lines += 1
    defs = len(re.findall(r'^\s*(?:async\s+)?(?:def|class)\s', text, re.M))
    # 有 docstring 的 def/class（下一非空行是三引号）
    documented = 0
    dl = text.splitlines()
    for i, l in enumerate(dl):
        if re.match(r'^\s*(?:async\s+)?(?:def|class)\s', l):
            for j in range(i + 1, min(i + 5, len(dl))):
                nxt = dl[j].strip()
                if not nxt:
                    continue
                if nxt.startswith('"""') or nxt.startswith("'''"):
                    documented += 1
                break
    ratio = (comment_lines + doc_lines) / total
    return {
        'path': str(path.relative_to(ROOT)).replace('\\', '/'),
        'total': total, 'comments': comment_lines, 'docs': doc_lines,
        'ratio': ratio, 'defs': defs, 'documented': documented,
    }


def main():
    min_lines = 20
    threshold = 0.10
    if '--min-lines' in sys.argv:
        min_lines = int(sys.argv[sys.argv.index('--min-lines') + 1])
    if '--threshold' in sys.argv:
        threshold = float(sys.argv[sys.argv.index('--threshold') + 1])

    rows = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', 'uploads', 'archive', 'reports')]
        for f in files:
            if f.endswith('.py'):
                r = audit_py(Path(root) / f)
                if r and r['total'] >= min_lines:
                    rows.append(r)

    rows.sort(key=lambda r: r['ratio'])
    print(f"{'文件':46s} {'行数':>5s} {'注释':>5s} {'文档':>5s} {'占比':>6s} {'函数/类':>7s} {'有文档':>6s}")
    print('-' * 88)
    for r in rows:
        flag = '  ← 需补充' if r['ratio'] < threshold else ''
        print(f"{r['path']:46s} {r['total']:5d} {r['comments']:5d} {r['docs']:5d} "
              f"{r['ratio']*100:5.1f}% {r['defs']:7d} {r['documented']:6d}{flag}")

    weak = [r for r in rows if r['ratio'] < threshold]
    undoc = [r for r in rows if r['defs'] > 0 and r['documented'] < r['defs']]
    print(f"\n合计 {len(rows)} 个文件；注释占比 <{threshold*100:.0f}% 的 {len(weak)} 个；"
          f"存在未写 docstring 函数的 {len(undoc)} 个")


if __name__ == '__main__':
    main()
