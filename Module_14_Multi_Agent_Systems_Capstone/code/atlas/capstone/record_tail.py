"""Record the closing scenes (run trace and Memory) as a second clip.

Read-only: it opens pages and scrolls; it never sends a message or decides an approval.
Writes capstone/video/tail.webm and tail_marks.json.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import asyncpg
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8011"
OUT = Path(__file__).resolve().parent.parent / "video"


async def main() -> None:
    from core.config import settings

    assert settings.pg_db == "atlas_capstone"
    conn = await asyncpg.connect(host=settings.pg_host, port=settings.pg_port, user=settings.pg_user,
                                 password=settings.pg_password, database=settings.pg_db)
    ep = await conn.fetchval("select id from episodes where agent_name='ceo' order by started_at limit 1")
    await conn.close()
    marks: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1,
                                        record_video_dir=str(OUT / "tail_tmp"),
                                        record_video_size={"width": 1280, "height": 720}, color_scheme="dark")
        page = await ctx.new_page()
        t0 = time.monotonic()

        def mark(label: str) -> None:
            marks.append({"t": round(time.monotonic() - t0, 2), "label": label})
            print(marks[-1], flush=True)

        await page.goto(f"{BASE}/#run/{ep}", wait_until="networkidle")
        await page.wait_for_timeout(2500)
        mark("trace")
        await page.screenshot(path=str(OUT / "tail_trace.png"))
        await page.wait_for_timeout(7000)
        await page.mouse.move(640, 420)
        for _ in range(4):
            await page.mouse.wheel(0, 350)
            await page.wait_for_timeout(1800)
        await page.wait_for_timeout(2500)
        await page.keyboard.press("Escape")
        await page.goto(f"{BASE}/#memory", wait_until="networkidle")
        await page.wait_for_timeout(2500)
        mark("memory")
        await page.screenshot(path=str(OUT / "tail_memory.png"))
        await page.wait_for_timeout(8000)
        mark("end")
        video = page.video
        await ctx.close()
        await browser.close()
        Path(await video.path()).replace(OUT / "tail.webm")
    (OUT / "tail_marks.json").write_text(json.dumps(marks, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
