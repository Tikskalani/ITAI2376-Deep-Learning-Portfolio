"""Render the capstone HTML documents to PDF with headless Chromium.

Usage (from the worktree root, with the main venv):
    ..\\.venv\\Scripts\\python.exe capstone/src/build_pdf.py proposal.html Trilok_Proposal_...pdf
Prints the page count so the 3-4 and 5-7 page limits can be checked.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright
from pypdf import PdfReader

SRC = Path(__file__).resolve().parent
OUT = SRC.parent / "out"

FOOTER = (
    '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:8px;color:#777;'
    'width:100%;padding:0 0.95in;display:flex;justify-content:space-between;">'
    "<span>Atlas: a multi-agent collaborative system &middot; Trilok Kalani &middot; ITAI 2376</span>"
    '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>'
)


async def render(html_name: str, pdf_name: str) -> int:
    OUT.mkdir(exist_ok=True)
    target = OUT / pdf_name
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto((SRC / html_name).as_uri(), wait_until="networkidle")
        await page.pdf(
            path=str(target),
            format="Letter",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=FOOTER,
            prefer_css_page_size=True,
        )
        await browser.close()
    pages = len(PdfReader(str(target)).pages)
    print(f"{target.name}: {pages} pages")
    return pages


if __name__ == "__main__":
    asyncio.run(render(sys.argv[1], sys.argv[2]))
