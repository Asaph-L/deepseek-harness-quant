# -*- coding: utf-8 -*-
"""发布包生成器（Release zip：全量含 HARNESS 运行时，解压即用）。

用法：
    python scripts/build_release.py [--version 1.0.8]

要点（历史教训）：
  - build/ 等目录名**只在仓库顶层排除**——node_modules 内各包的 build/ 目录（typebox/build、
    @opentelemetry/build 等）必须保留，否则 DSH 启动报 ERR_MODULE_NOT_FOUND
  - 不内嵌 zip/exe（EXE 作为单独 Release 附件）
"""
import argparse
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP_SKIP = {"build", "backups", "updates", "__pycache__", ".venv"}
# 内部维护文档（含本机路径，不进公开发布包）
SKIP_REL = {"docs/上传GitHub.md", "docs/发布清单_v1.0.9.md"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None)
    ap.add_argument("--out", default=str(Path.home() / "Desktop" / "quant-release"))
    args = ap.parse_args()
    ver = args.version or (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"DSHQuant-v{ver}-Release.zip"
    if out.exists():
        out.unlink()
    n = 0
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(ROOT):
            if root == str(ROOT):
                dirs[:] = [d for d in dirs if d not in TOP_SKIP]
            else:
                dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
            for f in files:
                if f.endswith((".zip", ".exe")):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                if rel in SKIP_REL:
                    continue
                z.write(p, "DSHQuant/" + rel)
                n += 1
    print(f"release: {out}  ({n} files / {out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
