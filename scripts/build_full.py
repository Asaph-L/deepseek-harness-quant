# -*- coding: utf-8 -*-
"""Windows 全环境发布包生成器（代码 + HARNESS 运行时 + 便携 Python + 便携 Node，解压即用）。

用法：
    python scripts/build_full.py --py <便携venv> --node <便携node目录> [--version 1.0.8]

历史教训：
  - build/ dist/ 等目录名**只在各自来源树的顶层排除**——node_modules 内各包的
    dist/（js-yaml/dist 等）、build/（typebox/build 等）必须保留，否则 DSH 报 ERR_MODULE_NOT_FOUND
"""
import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_SKIP = {"build", "backups", "updates", "__pycache__", ".venv", "dist"}


def copy_tree(src: Path, dst: Path):
    for root, dirs, files in os.walk(src):
        if root == str(src):
            dirs[:] = [d for d in dirs if d not in TOP_SKIP]
        else:
            dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        rel = os.path.relpath(root, src)
        tgt = dst if rel == "." else dst / rel
        tgt.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), tgt / f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", required=True, help="便携 Python venv 目录")
    ap.add_argument("--node", required=True, help="便携 Node 目录（node.exe 所在）")
    ap.add_argument("--version", default=None)
    ap.add_argument("--out", default=r"D:\quant-release")
    args = ap.parse_args()
    ver = args.version or (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    stage = Path(r"D:\quant-release\_full_stage\DSHQuant")
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    copy_tree(ROOT, stage)
    copy_tree(Path(args.py), stage / "runtime" / "python")
    copy_tree(Path(args.node), stage / "runtime" / "node")
    out = Path(args.out) / f"DSHQuant-v{ver}-Windows-Full.zip"
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(stage):
            dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, stage).replace("\\", "/")
                z.write(p, "DSHQuant/" + rel)
                n += 1
    shutil.rmtree(stage, ignore_errors=True)
    print(f"full: {out}  ({n} files / {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
