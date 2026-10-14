"""
FastAPI web server for the Amazon Read Sample scraper.
Run: uvicorn app:app --reload
"""

import asyncio
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scraper import resolve_and_open, scrape_reader

app = FastAPI(title="Amazon Sample Scraper")

OUTPUT_ROOT = Path("output")
OUTPUT_ROOT.mkdir(exist_ok=True)

# In-memory job store: job_id -> {"status", "pages", "asin", "error"}
jobs: dict[str, dict] = {}


# ------------------------------------------------------------------ #
# Background scrape task
# ------------------------------------------------------------------ #

async def run_scrape(job_id: str, url: str):
    from playwright.async_api import async_playwright

    jobs[job_id]["status"] = "running"
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            captured: dict[int, bytes] = {}

            async def on_response(response):
                if "cloudfront.net" not in response.url:
                    return
                m = re.search(r"\.S(\d{3})\.", response.url)
                if not m:
                    return
                pn = int(m.group(1))
                if pn in captured:
                    return
                try:
                    body = await response.body()
                    if body:
                        captured[pn] = body
                except Exception:
                    pass

            context.on("response", on_response)

            reader_page, asin = await resolve_and_open(page, url)
            output_dir = OUTPUT_ROOT / asin
            total = await scrape_reader(reader_page, output_dir, captured)

            await browser.close()

        jobs[job_id].update({"status": "done", "asin": asin, "pages": total})

    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


# ------------------------------------------------------------------ #
# API routes
# ------------------------------------------------------------------ #

class ScrapeRequest(BaseModel):
    url: str


@app.post("/scrape")
async def start_scrape(req: ScrapeRequest):
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "asin": None, "pages": 0, "error": None}
    asyncio.create_task(run_scrape(job_id, req.url))
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/pages/{asin}")
async def list_pages(asin: str):
    folder = OUTPUT_ROOT / asin
    if not folder.exists():
        raise HTTPException(status_code=404, detail="No output for this ASIN")
    files = sorted(f.name for f in folder.glob("*.jpg"))
    return {"asin": asin, "count": len(files), "files": files}


@app.get("/pages/{asin}/{filename}")
async def get_page_image(asin: str, filename: str):
    path = OUTPUT_ROOT / asin / filename
    if not path.exists() or path.suffix != ".jpg":
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(path, media_type="image/jpeg")


# ------------------------------------------------------------------ #
# Frontend
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=Path("static/index.html").read_text())


app.mount("/static", StaticFiles(directory="static"), name="static")
