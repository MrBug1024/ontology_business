"""从 seed_bookkeeping.py 提取 4 个文档文本，导出为 .md 文件供前端上传"""
import ast
from pathlib import Path

SEED = Path(__file__).resolve().parent.parent / "app" / "seed_bookkeeping.py"
OUT = Path(__file__).resolve().parent / "bookkeeping_docs"
OUT.mkdir(exist_ok=True)

src = SEED.read_text(encoding="utf-8")
tree = ast.parse(src)

samples = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "_ensure_file_bucket":
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name) and t.id == "samples":
                        samples = ast.literal_eval(sub.value)

assert samples, "samples dict not found"
for name, content in samples.items():
    (OUT / name).write_text(content, encoding="utf-8")
    print(f"OK: {OUT / name}  ({len(content)} chars)")
