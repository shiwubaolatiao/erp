"""列出缺少 docstring 的函数/端点（精确到行号，便于定点补充）

用法: python tools/list_undocumented.py [文件路径...]
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check(path: Path):
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    lines = src.splitlines()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not ast.get_docstring(node):
                decorators = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in node.decorator_list:
                        if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                            decorators.append(f"{d.func.attr}")
                        elif isinstance(d, ast.Attribute):
                            decorators.append(d.func.attr)
                tag = f"[{','.join(decorators)}]" if decorators else ""
                out.append((node.lineno, node.name, tag))
    return out


def main():
    targets = sys.argv[1:]
    files = []
    if targets:
        for t in targets:
            p = Path(t)
            files.append(p if p.is_absolute() else ROOT / t)
    else:
        for p in sorted(ROOT.rglob('app/**/*.py')):
            if '__pycache__' in str(p):
                continue
            files.append(p)

    total = 0
    for f in files:
        if not f.exists():
            continue
        rows = check(f)
        if not rows:
            continue
        total += len(rows)
        print(f"\n{f.relative_to(ROOT)}  ({len(rows)} 处缺注释)")
        for lineno, name, tag in rows:
            print(f"  L{lineno:<5d} {name}  {tag}")
    print(f"\n合计 {total} 处缺少 docstring")


if __name__ == '__main__':
    main()
