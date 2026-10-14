"""
FastAPI web server for the Amazon Read Sample scraper.
Run: uvicorn app:app --reload
"""

import asyncio
import io
import re
import sys
import uuid
import zipfile
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
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
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1280,900",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                java_script_enabled=True,
            )
            page = await context.new_page()
            # Stealth patches — mask all common headless detection signals
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'permissions', {
                    get: () => ({ query: (p) => Promise.resolve({state: 'granted'}) })
                });
            """)

            captured: dict[int, bytes] = {}

            async def on_response(response):
                if "cloudfront.net" not in response.url:
                    return
                m = re.search(r"\.S([0-9A-Z]{3})\.", response.url)
                if not m:
                    return
                pn = int(m.group(1), 36)
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
# Download routes
# ------------------------------------------------------------------ #

class DownloadRequest(BaseModel):
    files: list[str]  # list of filenames, e.g. ["page_001.jpg", "page_003.jpg"]


def _resolve_files(asin: str, filenames: list[str]) -> list[Path]:
    folder = OUTPUT_ROOT / asin
    if not folder.exists():
        raise HTTPException(status_code=404, detail="No output for this ASIN")
    paths = []
    for name in filenames:
        p = folder / name
        if not p.exists() or p.suffix != ".jpg":
            raise HTTPException(status_code=404, detail=f"File not found: {name}")
        paths.append(p)
    return paths


@app.post("/download/{asin}/zip")
async def download_zip(asin: str, req: DownloadRequest):
    paths = _resolve_files(asin, req.files)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, p.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={asin}_pages.zip"},
    )


@app.post("/download/{asin}/pdf")
async def download_pdf(asin: str, req: DownloadRequest):
    paths = _resolve_files(asin, req.files)
    images = [Image.open(p).convert("RGB") for p in paths]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={asin}_pages.pdf"},
    )


# ------------------------------------------------------------------ #
# Frontend
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=Path("static/index.html").read_text())


app.mount("/static", StaticFiles(directory="static"), name="static")
