"""CLI: python -m scripts.run_meta_eval [days]"""
from __future__ import annotations

import asyncio
import sys

from core.meta_eval import run_meta_eval


async def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    report = await run_meta_eval(days)
    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())
