"""CLI 自测 — 运行 menupilot --self-test 验证端到端管线。"""

import subprocess
import sys


def test_cli_self_test():
    """运行 menupilot --self-test 并验证退出码为 0。"""
    result = subprocess.run(
        [sys.executable, "-m", "menupilot", "--self-test"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    print(output)

    if "=== 结果:" in output:
        # 提取 pass/fail 统计
        for line in output.splitlines():
            if "passed" in line and "failed" in line:
                print(f"\n[TEST RESULT] {line.strip()}")

    assert result.returncode == 0, f"menupilot --self-test failed with exit code {result.returncode}"
    print("\n✅ CLI 自测通过")


if __name__ == "__main__":
    test_cli_self_test()
