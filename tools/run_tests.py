"""每个离线脚本在独立解释器运行，兼容脚本式断言和 unittest。"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parent.parent
    paths = sorted((root / "tests").glob("test_*.py")) + [root / "tests" / "fuzz_protocol.py"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    failed = []
    for path in paths:
        result = subprocess.run([sys.executable, "-B", str(path)], cwd=str(root),
                                capture_output=True, text=True, encoding="utf-8", env=env,
                                timeout=30)
        print("%s %s" % ("PASS" if result.returncode == 0 else "FAIL", path.name))
        if result.returncode:
            failed.append(path.name)
            print(result.stdout)
            print(result.stderr)
    print("%d scripts, %d failed" % (len(paths), len(failed)))
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
