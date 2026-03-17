#!/usr/bin/env python3
from __future__ import annotations
"""
BPCL Retail Outlet Scraper  v7  —  FIXED FULL VERSION + RESUME
===============================================================
All bugs from v6-HTML are fixed. Run bpcl_test.py first to
verify connectivity and HTML structure before running this.

Bug fixes vs v6-HTML:
  ✓ Bug 1/2 — parse_delta panel key: now searches ALL panels for <select>
  ✓ Bug 3   — ScriptManager field value format corrected
  ✓ Bug 4   — ViewState freshness: process_job always loads fresh VS
  ✓ Bug 5   — Hidden dict isolation between retries

Resume logic (new in v7r):
  ✓ Completed jobs written to  progress/done_jobs.txt  (one key per line)
  ✓ Discovered jobs cached to  progress/jobs_cache.json
  ✓ On restart: jobs cache reloaded (skips Phase 1), done jobs skipped
  ✓ Thread-safe file writes via asyncio.Lock
  ✓ --reset flag wipes progress and starts fresh

Output structure:
    html_pages/<State>/<District>/<PinCode>.html
    html_pages/<State>/<District>/<PinCode>_submit.html

Requirements:
    pip install aiohttp aiofiles beautifulsoup4 lxml tqdm

Run:
    python bpcl_scraper_v7r.py           # normal / resume run
    python bpcl_scraper_v7r.py --reset   # wipe progress, start fresh
"""

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

import aiofiles
import aiohttp
from bs4 import BeautifulSoup

try:
    from tqdm import tqdm as _tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ═══════════════════════════════════════════════════════
#  SPEED SETTINGS (conservative to avoid IP blocking)
# ═══════════════════════════════════════════════════════
DISCOVER_WORKERS = 3
SCRAPE_WORKERS   = 3
DELAY_S          = 2.0
PAGE_TIMEOUT     = 60
MAX_RETRIES      = 5
CONN_LIMIT       = 6

# Set via --proxy flag (e.g. http://ip:port)
PROXY_URL = None

# ═══════════════════════════════════════════════════════
BASE_URL  = "https://www.bharatpetroleum.in"
LIST_URL  = f"{BASE_URL}/our-businesses/fuels-and-services/retail-outlet-list"
VIEW_URL  = f"{BASE_URL}/our-businesses/fuels-and-services/retail-outlet-list-view"
SM_FIELD  = "ctl00$ContentPlaceHolder1$ScriptManager1"

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
]

def get_random_ua():
    return random.choice(UAS)

def _base_headers(ua=None):
    if not ua:
        ua = get_random_ua()
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

OUTPUT_DIR   = Path("html_pages")
PROGRESS_DIR = Path("progress")
DONE_FILE    = PROGRESS_DIR / "done_jobs.txt"
JOBS_CACHE   = PROGRESS_DIR / "jobs_cache.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bpcl")


# ═══════════════════════════════════════════════════════
#  DATA MODEL
# ═══════════════════════════════════════════════════════
@dataclass
class Job:
    state_val : str
    state_name: str
    dist_val  : str
    dist_name : str
    pin_val   : str
    pin_name  : str

    def key(self) -> str:
        """Unique stable identifier for this job used in the done-set."""
        return f"{self.state_val}|{self.dist_val}|{self.pin_val}"


def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', '_', s.strip()).strip('_') or "unknown"


# ═══════════════════════════════════════════════════════
#  RESUME HELPERS
# ═══════════════════════════════════════════════════════

def load_done_set() -> set[str]:
    """Load the set of already-completed job keys from disk."""
    if not DONE_FILE.exists():
        return set()
    with DONE_FILE.open("r", encoding="utf-8") as f:
        keys = {line.strip() for line in f if line.strip()}
    log.info("  Resuming — %d jobs already done (loaded from %s)",
             len(keys), DONE_FILE)
    return keys


async def mark_done(key: str, file_lock: asyncio.Lock):
    """Append a completed job key to the done file. Thread-safe."""
    async with file_lock:
        async with aiofiles.open(DONE_FILE, "a", encoding="utf-8") as f:
            await f.write(key + "\n")


def save_jobs_cache(jobs: list[Job]):
    """Serialize the full job list to JSON so Phase 1 can be skipped on resume."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_CACHE.open("w", encoding="utf-8") as f:
        json.dump([asdict(j) for j in jobs], f, ensure_ascii=False, indent=2)
    log.info("  Jobs cache saved → %s (%d jobs)", JOBS_CACHE, len(jobs))


def load_jobs_cache() -> list[Job] | None:
    """Load jobs from cache. Returns None if cache doesn't exist."""
    if not JOBS_CACHE.exists():
        return None
    with JOBS_CACHE.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    jobs = [Job(**d) for d in raw]
    log.info("  Jobs cache loaded → %d total jobs (skipping Phase 1)", len(jobs))
    return jobs


def reset_progress():
    """Wipe all progress files to start completely fresh."""
    for p in [DONE_FILE, JOBS_CACHE]:
        if p.exists():
            p.unlink()
            log.info("  Deleted %s", p)
    log.info("  Progress reset — will run from scratch.")





# ═══════════════════════════════════════════════════════
#  PARSING HELPERS  (unchanged)
# ═══════════════════════════════════════════════════════

def parse_hidden(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    return {
        inp["name"]: inp.get("value", "")
        for inp in soup.find_all("input", {"type": "hidden"})
        if inp.get("name")
    }


def parse_delta(text: str) -> tuple[dict, dict]:
    panels, hidden = {}, {}
    pos, n = 0, len(text)
    while pos < n:
        p1 = text.find("|", pos)
        if p1 == -1: break
        try:
            seg_len = int(text[pos:p1])
        except ValueError:
            pos = p1 + 1; continue
        p2 = text.find("|", p1 + 1)
        if p2 == -1: break
        p3 = text.find("|", p2 + 1)
        if p3 == -1: break
        seg_type = text[p1 + 1:p2]
        seg_id   = text[p2 + 1:p3]
        cs, ce   = p3 + 1, p3 + 1 + seg_len
        if ce > n: break
        content  = text[cs:ce]
        if   seg_type == "updatePanel": panels[seg_id] = content
        elif seg_type == "hiddenField": hidden[seg_id] = content
        pos = ce + 1
    return panels, hidden


def _get_opts_from_html(html: str, sel_id: str) -> list[dict]:
    SKIP = {"", "0", "--select state--", "--select district--",
            "--select pin--", "--select--"}
    soup = BeautifulSoup(html, "lxml")
    el = (soup.find("select", id=sel_id) or
          soup.find("select", attrs={"name": sel_id}))
    if not el:
        return []
    return [
        {"value": o.get("value", "").strip(),
         "text":  o.get_text(strip=True)}
        for o in el.find_all("option")
        if o.get("value", "").strip().lower() not in SKIP
        and o.get("value", "").strip()
    ]


def get_opts_from_page(page_html: str, sel_id: str) -> list[dict]:
    return _get_opts_from_html(page_html, sel_id)


def get_opts(panels: dict, sel_id: str) -> list[dict]:
    for html in panels.values():
        opts = _get_opts_from_html(html, sel_id)
        if opts:
            return opts
    return []


# ═══════════════════════════════════════════════════════
#  FORM BUILDER  (unchanged)
# ═══════════════════════════════════════════════════════

def mk_form(hidden: dict, target: str, extra: dict = None) -> dict:
    f = dict(hidden)
    f["__EVENTTARGET"]   = target
    f["__EVENTARGUMENT"] = ""
    f[SM_FIELD]          = f"{SM_FIELD}|{target}"
    if extra:
        f.update(extra)
    return f


# ═══════════════════════════════════════════════════════
#  HTTP LAYER  (unchanged)
# ═══════════════════════════════════════════════════════

def _jitter(base: float) -> float:
    """Add ±30% random jitter to a delay value."""
    return base * (0.7 + random.random() * 0.6)


# Removed redundant _base_headers


def _ajax_headers(body_len: int, ua=None):
    return {
        **_base_headers(ua),
        "Accept"          : "*/*",
        "Origin"          : BASE_URL,
        "Referer"         : LIST_URL,
        "X-Requested-With": "XMLHttpRequest",
        "X-MicrosoftAjax": "Delta=true",
        "Content-Type"    : "application/x-www-form-urlencoded; charset=utf-8",
        "Content-Length"  : str(body_len),
    }


RETRY_CODES = {403, 429, 502, 503, 504}


async def do_get(session: aiohttp.ClientSession, url: str, att: int = 0, ua: str | None = None) -> str | None:
    try:
        current_ua = ua or get_random_ua()
        kwargs = dict(
            headers=_base_headers(current_ua),
            timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT),
        )
        if PROXY_URL:
            kwargs["proxy"] = PROXY_URL
        async with session.get(url, **kwargs) as r:
            if r.status == 200:
                return await r.text()
            if r.status in RETRY_CODES and att < MAX_RETRIES:
                wait = _jitter(3.0 * (2 ** att))
                log.info("GET HTTP %d — retry %d/%d in %.1fs (rotating UA)", r.status, att + 1, MAX_RETRIES, wait)
                await asyncio.sleep(wait)
                return await do_get(session, url, att + 1, get_random_ua())
            log.warning("GET %s → HTTP %d (gave up after %d attempts)", url, r.status, att + 1)
            return None
    except Exception as e:
        if att < MAX_RETRIES:
            wait = _jitter(2.0 * (2 ** att))
            await asyncio.sleep(wait)
            return await do_get(session, url, att + 1, get_random_ua())
        log.warning("GET %s failed after %d attempts: %s", url, att + 1, e)
        return None


async def do_post(session: aiohttp.ClientSession, form: dict, att: int = 0, ua: str | None = None) -> str | None:
    current_ua = ua or get_random_ua()
    body = urllib.parse.urlencode(form, encoding="utf-8")
    try:
        kwargs = dict(
            data=body,
            headers=_ajax_headers(len(body.encode()), current_ua),
            timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT),
        )
        if PROXY_URL:
            kwargs["proxy"] = PROXY_URL
        async with session.post(LIST_URL, **kwargs) as r:
            if r.status == 200:
                return await r.text()
            if r.status in RETRY_CODES and att < MAX_RETRIES:
                wait = _jitter(3.0 * (2 ** att))
                log.info("POST HTTP %d — retry %d/%d in %.1fs (rotating UA)", r.status, att + 1, MAX_RETRIES, wait)
                await asyncio.sleep(wait)
                return await do_post(session, form, att + 1, get_random_ua())
            log.warning("POST → HTTP %d (gave up after %d attempts)", r.status, att + 1)
            return None
    except Exception as e:
        if att < MAX_RETRIES:
            wait = _jitter(2.0 * (2 ** att))
            await asyncio.sleep(wait)
            return await do_post(session, form, att + 1, get_random_ua())
        log.warning("POST failed after %d attempts: %s", att + 1, e)
        return None


# ═══════════════════════════════════════════════════════
#  FILE SAVER  (unchanged)
# ═══════════════════════════════════════════════════════

async def save_html(state: str, district: str, filename: str, content: str):
    folder = OUTPUT_DIR / _safe_name(state) / _safe_name(district)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_name(filename)}.html"
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)


# ═══════════════════════════════════════════════════════
#  PHASE 1 — DISCOVER JOBS  (unchanged)
# ═══════════════════════════════════════════════════════

async def discover_state(
    state  : dict,
    session: aiohttp.ClientSession,
    sem    : asyncio.Semaphore,
    jobs   : list,
    lock   : asyncio.Lock,
):
    async with sem:
        page = await do_get(session, LIST_URL)
        if not page:
            log.warning("Skip state %s — page load failed", state["text"])
            return
        h = parse_hidden(page)

        await asyncio.sleep(DELAY_S)
        r1 = await do_post(session, mk_form(h, "ddlState",
                                            {"ddlState": state["value"]}))
        if not r1:
            log.warning("Skip state %s — state POST failed", state["text"])
            return
        panels1, h1 = parse_delta(r1)
        h.update(h1)

        dist_opts = get_opts(panels1, "ddlDistrict")
        if not dist_opts:
            log.warning("No districts for state: %s", state["text"])
            return

        new_jobs: list[Job] = []

        for dist in dist_opts:
            await asyncio.sleep(DELAY_S)
            r2 = await do_post(session, mk_form(h, "ddlDistrict", {
                "ddlState"   : state["value"],
                "ddlDistrict": dist["value"],
            }))
            if not r2:
                continue
            panels2, h2 = parse_delta(r2)
            h.update(h2)

            pin_opts = get_opts(panels2, "ddlPinCode")
            if pin_opts:
                for pin in pin_opts:
                    new_jobs.append(Job(
                        state["value"], state["text"],
                        dist["value"],  dist["text"],
                        pin["value"],   pin["text"],
                    ))
            else:
                new_jobs.append(Job(
                    state["value"], state["text"],
                    dist["value"],  dist["text"],
                    "",             "_no_pin",
                ))

        async with lock:
            jobs.extend(new_jobs)

        log.info("✓ %-25s %3d districts  %4d pin-jobs",
                 state["text"], len(dist_opts), len(new_jobs))


async def build_jobs(session: aiohttp.ClientSession) -> list[Job]:
    # ── Resume: skip Phase 1 entirely if cache exists ─────────────────────────
    cached = load_jobs_cache()
    if cached is not None:
        return cached

    log.info("Phase 1 — discovering jobs…")
    page = await do_get(session, LIST_URL)
    if not page:
        log.error("Cannot load page"); return []

    state_opts = get_opts_from_page(page, "ddlState")
    log.info("  States found: %d", len(state_opts))
    if not state_opts:
        log.error("No states found — check URL / page structure")
        return []

    jobs: list[Job] = []
    lock = asyncio.Lock()
    sem  = asyncio.Semaphore(DISCOVER_WORKERS)

    await asyncio.gather(*[
        discover_state(s, session, sem, jobs, lock)
        for s in state_opts
    ])

    log.info("Phase 1 done — %d total jobs", len(jobs))

    # ── Save cache immediately so future runs skip Phase 1 ────────────────────
    save_jobs_cache(jobs)

    return jobs


# ═══════════════════════════════════════════════════════
#  PHASE 2 — SCRAPE EACH JOB  (core unchanged, resume added)
# ═══════════════════════════════════════════════════════

async def process_job(job: Job, session: aiohttp.ClientSession) -> int:
    """
    Execute the full postback chain for one pincode and save HTML files.
    Returns number of files saved (0 on failure).
    Core logic is identical to v7 — only the caller adds resume skip/mark.
    """
    saved = 0

    async def _attempt() -> int:
        nonlocal saved

        page = await do_get(session, LIST_URL)
        if not page:
            return 0
        h = parse_hidden(page)

        await asyncio.sleep(_jitter(DELAY_S))
        r1 = await do_post(session, mk_form(h, "ddlState",
                                            {"ddlState": job.state_val}))
        if not r1: return 0
        _, h1 = parse_delta(r1); h.update(h1)

        await asyncio.sleep(_jitter(DELAY_S))
        r2 = await do_post(session, mk_form(h, "ddlDistrict", {
            "ddlState"   : job.state_val,
            "ddlDistrict": job.dist_val,
        }))
        if not r2: return 0
        _, h2 = parse_delta(r2); h.update(h2)

        if job.pin_val:
            await asyncio.sleep(_jitter(DELAY_S))
            r3 = await do_post(session, mk_form(h, "ddlPinCode", {
                "ddlState"   : job.state_val,
                "ddlDistrict": job.dist_val,
                "ddlPinCode" : job.pin_val,
            }))
            if r3:
                _, h3 = parse_delta(r3); h.update(h3)

        await asyncio.sleep(_jitter(DELAY_S))
        submit_form = dict(h)
        submit_form.update({
            "__EVENTTARGET"  : "",
            "__EVENTARGUMENT": "",
            "ddlState"       : job.state_val,
            "ddlDistrict"    : job.dist_val,
            "ddlPinCode"     : job.pin_val,
            "btnCheck"       : "Submit",
            SM_FIELD         : f"{SM_FIELD}|btnCheck",
        })
        r4 = await do_post(session, submit_form)

        await asyncio.sleep(_jitter(DELAY_S))
        view = await do_get(session, VIEW_URL)

        pin_label = job.pin_name or job.pin_val or "_no_pin"

        if view:
            await save_html(job.state_name, job.dist_name, pin_label, view)
            saved += 1
        if r4:
            await save_html(job.state_name, job.dist_name,
                            f"{pin_label}_submit", r4)
            saved += 1

        return saved

    try:
        return await _attempt()
    except Exception as e:
        log.debug("Job [%s/%s/%s] error: %s",
                  job.state_name, job.dist_name, job.pin_name, e)
        return 0


# ═══════════════════════════════════════════════════════
#  WORKER POOL  (resume: skip done, mark on success)
# ═══════════════════════════════════════════════════════

async def scrape_worker(
    wid      : int,
    queue    : asyncio.Queue,
    session  : aiohttp.ClientSession,
    stats    : dict,
    done_set : set[str],
    file_lock: asyncio.Lock,
    pbar,
):
    while True:
        try:
            job: Job = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        try:
            jkey = job.key()

            # ── RESUME: skip already-completed jobs ───────────────────────────
            if jkey in done_set:
                stats["skipped"] += 1
                if pbar:
                    pbar.update(1)
                    pbar.set_postfix(
                        saved=stats["saved"],
                        skip=stats["skipped"],
                        refresh=False,
                    )
                continue

            n = await process_job(job, session)
            stats["saved"] += n

            # ── RESUME: persist success to done file ──────────────────────────
            if n > 0:
                await mark_done(jkey, file_lock)
                done_set.add(jkey)   # keep in-memory set in sync

        except Exception as e:
            log.debug("Worker %d unhandled: %s", wid, e)
        finally:
            queue.task_done()
            stats["done"] += 1
            if pbar:
                pbar.update(1)
                pbar.set_postfix(
                    saved=stats["saved"],
                    skip=stats["skipped"],
                    refresh=False,
                )
            elif stats["done"] % 50 == 0:
                log.info(
                    "  Progress: %d/%d jobs | saved %d files | skipped %d",
                    stats["done"], stats["total"],
                    stats["saved"], stats["skipped"],
                )


async def run_scrape(
    jobs     : list[Job],
    session  : aiohttp.ClientSession,
    done_set : set[str],
):
    pending = len(jobs) - len(done_set & {j.key() for j in jobs})
    log.info(
        "Phase 2 — %d total jobs | %d already done | %d to scrape | %d workers",
        len(jobs), len(done_set), pending, SCRAPE_WORKERS,
    )

    queue: asyncio.Queue = asyncio.Queue()
    for job in jobs:
        await queue.put(job)

    stats     = {"done": 0, "saved": 0, "skipped": 0, "total": len(jobs)}
    file_lock = asyncio.Lock()   # serialises writes to done_jobs.txt

    pbar = None
    if HAS_TQDM:
        pbar = _tqdm(
            total=len(jobs), desc="Scraping",
            unit="job", dynamic_ncols=True,
            initial=0,
            postfix={"saved": 0, "skip": 0},
        )

    workers = [
        asyncio.create_task(
            scrape_worker(i, queue, session, stats,
                          done_set, file_lock, pbar)
        )
        for i in range(min(SCRAPE_WORKERS, len(jobs)))
    ]

    await asyncio.gather(*workers)

    if pbar:
        pbar.close()

    log.info(
        "Phase 2 done — %d files saved | %d skipped (already done) | %d failed",
        stats["saved"],
        stats["skipped"],
        stats["done"] - stats["skipped"] - (stats["saved"] // 2),
    )


# ═══════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════

async def main(reset: bool, proxy: str | None = None):
    global PROXY_URL
    t0 = time.time()

    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if reset:
        reset_progress()

    if proxy:
        PROXY_URL = proxy

    done_set = load_done_set()

    log.info("╔══════════════════════════════════════════╗")
    log.info("║   BPCL Scraper v7r — Resume-Enabled      ║")
    log.info("║   Scrape workers    : %-3d                ║", SCRAPE_WORKERS)
    log.info("║   Delay per request : %.2fs              ║", DELAY_S)
    log.info("║   Output folder     : %-20s  ║", str(OUTPUT_DIR))
    log.info("║   Jobs already done : %-6d              ║", len(done_set))
    log.info("║   Proxy             : %-20s  ║", PROXY_URL or "DIRECT")
    log.info("╚══════════════════════════════════════════╝")

    connector = aiohttp.TCPConnector(
        limit=CONN_LIMIT, limit_per_host=CONN_LIMIT,
        ttl_dns_cache=300, enable_cleanup_closed=True,
    )
    cookie_jar = aiohttp.CookieJar()

    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=cookie_jar,
        connector_owner=True,
        headers=_base_headers(),
    ) as session:
        # ── Warm-up: load the page to get cookies & check access ─────────
        mode = f"via proxy {PROXY_URL}" if PROXY_URL else "direct"
        log.info("Warming up session (%s)…", mode)
        warmup = await do_get(session, LIST_URL)
        if not warmup:
            log.error("Cannot reach BPCL site (%s).", mode)
            if not PROXY_URL:
                log.error("Your IP may be blocked. Try: python bpcl.py --proxy http://IP:PORT")
            else:
                log.error("The proxy isn't working. Try a different one.")
            return
        log.info("Warm-up OK — cookies established (%s).", mode)
        await asyncio.sleep(2)

        jobs = await build_jobs(session)
        if not jobs:
            log.error("No jobs found — run bpcl_test.py first to diagnose")
            return
        await run_scrape(jobs, session, done_set)

    elapsed = time.time() - t0
    log.info("══════════════════════════════════════════")
    log.info("  Total time  : %.0fs  (%.1fmin)", elapsed, elapsed / 60)
    log.info("  HTML saved in: %s", OUTPUT_DIR.resolve())
    log.info("  Progress in  : %s", PROGRESS_DIR.resolve())
    log.info("══════════════════════════════════════════")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BPCL Scraper v7r")
    parser.add_argument(
        "--reset", action="store_true",
        help="Wipe progress files and start from scratch",
    )
    parser.add_argument(
        "--proxy", type=str, default=None,
        help="HTTP proxy URL, e.g. http://IP:PORT",
    )
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(reset=args.reset, proxy=args.proxy))