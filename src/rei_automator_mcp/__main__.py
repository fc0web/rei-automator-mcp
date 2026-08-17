"""rei-automator-mcp のエントリポイント。

`rei-automator-mcp` コマンド、および `python -m rei_automator_mcp` の
両方から呼ばれる。
"""

from __future__ import annotations

import sys

from . import _register_mcp, _selftest


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    server = _register_mcp()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
