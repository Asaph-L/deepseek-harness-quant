# -*- coding: utf-8 -*-
"""DeepSeek API 一键接入向导：填写 Key → 写入配置 → 完成。

双击「接入API.cmd」运行本脚本。
"""
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CRED = BASE / "harness" / "home" / ".credentials.yaml"


def main():
    print("=" * 52)
    print("  DeepSeek HARNESS Quant · API 一键接入")
    print("=" * 52)

    # 1. 检测 HARNESS 运行时（只有完整包 zip 才有）
    if not (BASE / "harness" / "node_modules").exists():
        print()
        print("[!] 未检测到 HARNESS 运行时。")
        print("    单文件 exe 不含 HARNESS，请改用 zip 完整包。")
        input("按回车退出...")
        return

    # 2. 检测 Node.js
    if not shutil.which("node"):
        print()
        print("[!] 未检测到 Node.js 18+。HARNESS 需要 Node.js 才能启动。")
        print("    下载安装：https://nodejs.org")
        print("    装好后重新运行本程序。")
        input("按回车退出...")
        return

    # 3. 已有配置提示
    if CRED.exists():
        print()
        print("[i] 已存在 .credentials.yaml，重新填写会覆盖旧 Key。")

    # 4. 输入 Key
    print()
    key = input("请输入 DeepSeek API Key（sk- 开头，粘贴后回车）：").strip()
    if not key.startswith("sk-") or len(key) < 20:
        print()
        print("[!] Key 格式不对，应为 sk- 开头的长字符串。")
        input("按回车退出...")
        return

    # 5. 写入配置
    CRED.write_text("DEEPSEEK_API_KEY: {}\n".format(key), encoding="utf-8")
    print()
    print("[OK] 已写入 {}".format(CRED.name))

    # 6. 可选验证（失败不阻塞，可能是网络问题）
    print("[..] 正在验证 Key ...")
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/models",
            headers={"Authorization": "Bearer " + key},
        )
        urllib.request.urlopen(req, timeout=10)
        print("[OK] Key 验证通过")
    except urllib.error.HTTPError as e:
        print("[!] Key 验证失败（HTTP {}）—— Key 可能无效".format(e.code))
    except Exception:
        print("[i] 验证超时/网络不可达 —— Key 已保存，网络恢复后可用")

    # 7. 完成
    print()
    print("接入完成。下一步：")
    print("  1. 运行 launcher.py（或双击 启动.cmd）")
    print("  2. 看到「启动 DeepSeek HARNESS」即集成成功")
    print("  3. 打开 http://127.0.0.1:8787/control 对话")
    print()
    input("按回车退出...")


if __name__ == "__main__":
    main()
