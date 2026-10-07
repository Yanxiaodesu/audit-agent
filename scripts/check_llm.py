"""检查 LLM 配置是否可用。

用法：
    python scripts/check_llm.py

它会：
  1. 打印当前配置（key 只显示首尾各 4 位，不会完整打印）
  2. 如果配了 key，**真的发一次最小请求**验证能不能通
  3. 按错误码给出针对性的排查建议
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app import config as C  # noqa: E402


def mask(s: str) -> str:
    if not s:
        return "(空)"
    if len(s) <= 8:
        return s[:2] + "***"
    return f"{s[:4]}...{s[-4:]}  (长度 {len(s)})"


def main() -> int:
    print("=" * 64)
    print("  audit-agent  LLM 配置检查")
    print("=" * 64)
    print(f"  配置文件      {C.ENV_FILE}")
    print(f"                 {'存在' if C.ENV_FILE.exists() else '不存在'}")
    print(f"  LLM_MODE      {C.LLM_MODE}")
    print(f"  LLM_ENABLED   {C.LLM_ENABLED}")
    print(f"  LLM_API_KEY   {mask(C.LLM_API_KEY)}")
    print(f"  LLM_BASE_URL  {C.LLM_BASE_URL}")
    print(f"  LLM_MODEL     {C.LLM_MODEL}")

    if C.LLM_MODE == "mock":
        print("\n  [OK] mock 模式：不需要 key。")
        print("       循环代码会用脚本化响应真实跑一遍（工具调用/终止条件都会被覆盖）。")
        return 0

    if not C.LLM_API_KEY:
        print("\n  [!] 没有配置 LLM_API_KEY")
        print("      项目仍可完整运行，审核走「规则引擎 + 人工复核」，只是模型层不启用。")
        print(f"\n      要启用模型层，编辑这个文件：\n        {C.ENV_FILE}")
        print("      把这一行的等号后面填上你的 key：")
        print("        LLM_API_KEY=sk-xxxxxxxx")
        print("\n      然后重启服务（关掉 start-all.cmd 的两个窗口，再双击一次）。")
        print("      提示：想先不花 key 验证模型层代码，可以临时设 LLM_MODE=mock")
        return 0

    print("\n  发一次最小请求验证连通性 ...")
    try:
        r = httpx.post(
            f"{C.LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {C.LLM_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": C.LLM_MODEL,
                  "messages": [{"role": "user", "content": "回复两个字：可用"}],
                  "max_tokens": 16, "temperature": 0},
            timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        print(f"\n  [X] 连不上 {C.LLM_BASE_URL}")
        print(f"      {type(e).__name__}: {e}")
        print("      排查：base_url 是否写错 / 网络是否可达 / 是否需要代理")
        return 1

    if r.status_code == 200:
        data = r.json()
        content = (data["choices"][0]["message"].get("content") or "").strip()
        usage = data.get("usage") or {}
        print("\n  [OK] 调用成功")
        print(f"       模型回复  : {content[:40]}")
        print(f"       token 用量: {usage.get('total_tokens')}")
        print(f"       实际模型  : {data.get('model')}")
        print("\n  配置可用，重启服务即可启用模型层。")
        return 0

    print(f"\n  [X] HTTP {r.status_code}")
    print(f"      {r.text[:400]}")
    hints = {
        401: "key 无效、已过期，或复制时带了空格",
        402: "账户余额不足，需要充值",
        403: "key 没有该模型的权限",
        404: f"模型名 '{C.LLM_MODEL}' 在该服务上不存在 —— 检查 LLM_MODEL 是否写对",
        429: "触发限流，稍后重试或降低并发",
        500: "服务端错误，稍后重试",
    }
    if r.status_code in hints:
        print(f"      -> {hints[r.status_code]}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
