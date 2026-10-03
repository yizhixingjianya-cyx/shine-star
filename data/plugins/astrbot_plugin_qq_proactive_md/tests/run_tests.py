"""一次性运行插件全部联调测试。

    python tests/run_tests.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    """发现并运行 tests 目录下的全部测试。

    Returns:
        unittest 退出码：0 表示全部通过。
    """
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(TESTS_DIR), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
