# Amazon Sample Scraper

A local web tool that scrapes the "Read Sample" pages from any Amazon book listing and displays them in a browser gallery.

---

## Requirements

- Python 3.8 or newer
- macOS, Windows, or Linux

---

## Setup & Installation

### 1. Clone the repository

```bash
git clone https://github.com/SreeHari-WM/amazon-sample-scraper.git
cd amazon-sample-scraper
```

### 2. Install Python dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Install the Chromium browser (one-time)

```bash
python3 -m playwright install chromium
```

### 4. Start the server

```bash
python3 -m uvicorn app:app --reload
```

### 5. Open the tool

Open your browser and go to:

```
http://localhost:8000
```

---

## How to use

1. Find any Amazon book page (e.g. `https://www.amazon.com/dp/B08N5WRWNW`) or use a short link (`https://a.co/d/...`)
2. Paste the URL into the input field and click **Scrape**
3. A Chromium browser window will open automatically — do not close it
4. Once the scrape finishes, the sample pages appear as a grid in the tool
5. Click any page to view it full-size

---

## Notes

- The browser window that opens is intentional — Amazon blocks headless browsers
- If Amazon shows a CAPTCHA, solve it in the browser window and the scrape will continue automatically
- Scraped images are saved to `output/<ASIN>/` on your machine
