#!/usr/bin/env python3
"""
BPCL Retail Outlet Scraper — TEST VERSION (single state)
=========================================================
Runs against ONE small state to verify the scraper works end-to-end
before launching the full multi-state run.

All major bugs from v6-HTML are fixed here:
  Bug 1 — parse_delta panel-key: p1.get("update","") always returned ""
           Fix: search all panel values for the <select> element
  Bug 2 — get_opts returned [] because of Bug 1 → no districts/pins found
  Bug 3 — ScriptManager field value wrong format ("update|X" vs correct)
  Bug 4 — ViewState staleness: discovery VS expired by scrape time
           Fix: always do a fresh GET in process_job
  Bug 5 — stale hidden dict reuse across retry in run_with_hidden

Usage:
    pip install aiohttp aiofiles beautifulsoup4 lxml
    python bpcl_test.py

Output:
    test_html/<State>/<District>/<Pin>.html
    test_html/<State>/<District>/<Pin>_submit.html
"""

import asyncio
import logging
import re
import sys
import time
import urllib.parse
from pathlib import Path

import aiofiles
import aiohttp
from bs4 import BeautifulSoup

# ── CONFIG ─────────────────────────────────────────────────────────────────────
# Change this to any small state name (partial match, case-insensitive)
# Examples: "Goa", "Andaman", "Chandigarh", "Lakshadweep", "Dadra"
TARGET_STATE  = "Goa"

OUTPUT_DIR    = Path("test_html")
DELAY_S       = 0.3          # slower for test — be polite
PAGE_TIMEOUT  = 30
MAX_RETRIES   = 3

BASE_URL  = "https://www.bharatpetroleum.in"
LIST_URL  = f"{BASE_URL}/our-businesses/fuels-and-services/retail-outlet-list"
VIEW_URL  = f"{BASE_URL}/our-businesses/fuels-and-services/retail-outlet-list-view"

# ASP.NET ScriptManager field name (stays constant on this page)
SM_FIELD  = "ctl00$ContentPlaceHolder1$ScriptManager1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) "
      "Gecko/20100101 Firefox/148.0")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bpcl_test")


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', '_', s.strip()).strip('_') or "unknown"


def parse_hidden(html: str) -> dict:
    """Extract all hidden inputs from a full HTML page."""
    soup = BeautifulSoup(html, "lxml")
    hidden = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "").strip()
        if name:
            hidden[name] = inp.get("value", "")
    log.debug("  parse_hidden → %d fields (__VS len=%d)",
              len(hidden), len(hidden.get("__VIEWSTATE", "")))
    return hidden


# ── FIX 1: parse_delta returns keyed panels dict ───────────────────────────────
def parse_delta(text: str) -> tuple[dict, dict]:
    """
    Parse ASP.NET ScriptManager UpdatePanel delta response.
    Format: LENGTH|TYPE|ID|CONTENT…
    Returns (panels_dict, hidden_fields_dict)
    """
    panels, hidden = {}, {}
    pos, n = 0, len(text)
    segments_found = 0

    while pos < n:
        p1 = text.find("|", pos)
        if p1 == -1:
            break
        try:
            seg_len = int(text[pos:p1])
        except ValueError:
            pos = p1 + 1
            continue
        p2 = text.find("|", p1 + 1)
        if p2 == -1:
            break
        p3 = text.find("|", p2 + 1)
        if p3 == -1:
            break

        seg_type = text[p1 + 1:p2]
        seg_id   = text[p2 + 1:p3]
        cs, ce   = p3 + 1, p3 + 1 + seg_len
        if ce > n:
            break
        content = text[cs:ce]

        if   seg_type == "updatePanel": panels[seg_id] = content
        elif seg_type == "hiddenField": hidden[seg_id] = content

        segments_found += 1
        pos = ce + 1   # skip the trailing "|"

    log.debug("  parse_delta → %d segments, panels=%s, hidden_keys=%s",
              segments_found, list(panels.keys()), list(hidden.keys()))
    return panels, hidden


# ── FIX 2: search ALL panels for the <select> ──────────────────────────────────
def get_opts(panels: dict, sel_id: str) -> list[dict]:
    """
    Search every update panel HTML chunk for a <select> with the given ID.
    Returns list of {value, text} dicts for non-placeholder options.
    """
    SKIP = {"", "0", "--select state--", "--select district--",
            "--select pin--", "--select--"}

    for panel_id, html in panels.items():
        soup = BeautifulSoup(html, "lxml")
        el = (soup.find("select", id=sel_id) or
              soup.find("select", attrs={"name": sel_id}))
        if el:
            opts = [
                {"value": o.get("value", "").strip(),
                 "text":  o.get_text(strip=True)}
                for o in el.find_all("option")
                if o.get("value", "").strip().lower() not in SKIP
                and o.get("value", "").strip()
            ]
            log.debug("  get_opts(%s) found in panel '%s' → %d options",
                      sel_id, panel_id, len(opts))
            return opts

    log.debug("  get_opts(%s) → NOT FOUND in any panel (%s)",
              sel_id, list(panels.keys()))
    return []


def get_opts_from_page(html: str, sel_id: str) -> list[dict]:
    """Same as get_opts but works on a full HTML page string (not delta)."""
    SKIP = {"", "0", "--select state--", "--select district--",
            "--select pin--", "--select--"}
    soup = BeautifulSoup(html, "lxml")
    el = (soup.find("select", id=sel_id) or
          soup.find("select", attrs={"name": sel_id}))
    if not el:
        log.debug("  get_opts_from_page(%s) → select not found", sel_id)
        return []
    opts = [
        {"value": o.get("value", "").strip(),
         "text":  o.get_text(strip=True)}
        for o in el.find_all("option")
        if o.get("value", "").strip().lower() not in SKIP
        and o.get("value", "").strip()
    ]
    log.debug("  get_opts_from_page(%s) → %d options", sel_id, len(opts))
    return opts


# ── FIX 3: correct ScriptManager field value format ───────────────────────────
def mk_form(hidden: dict, target: str, extra: dict = None) -> dict:
    """
    Build the POST form for an ASP.NET UpdatePanel partial postback.
    The ScriptManager field value must be: "PageID|TargetControlID"
    The typical format used in ASP.NET is the full UpdatePanel ClientID.
    """
    f = dict(hidden)
    f["__EVENTTARGET"]   = target
    f["__EVENTARGUMENT"] = ""
    # Correct SM field format: "ScriptManagerID|EventTargetID"
    f[SM_FIELD] = f"ctl00$ContentPlaceHolder1$ScriptManager1|{target}"
    if extra:
        f.update(extra)
    return f


# ── HTTP ───────────────────────────────────────────────────────────────────────
def _base_headers():
    return {
        "User-Agent"     : UA,
        "Accept"         : "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control"  : "no-cache",
        "Connection"     : "keep-alive",
    }

def _ajax_headers(body_len: int):
    return {
        **_base_headers(),
        "Accept"          : "*/*",
        "Origin"          : BASE_URL,
        "Referer"         : LIST_URL,
        "X-Requested-With": "XMLHttpRequest",
        "X-MicrosoftAjax": "Delta=true",
        "Content-Type"    : "application/x-www-form-urlencoded; charset=utf-8",
        "Content-Length"  : str(body_len),
    }


async def do_get(session: aiohttp.ClientSession, url: str, attempt: int = 0) -> str | None:
    try:
        async with session.get(
            url, headers=_base_headers(),
            timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT)
        ) as r:
            log.debug("  GET %s → %d", url, r.status)
            if r.status == 200:
                return await r.text()
            if r.status in (429, 502, 503) and attempt < MAX_RETRIES:
                await asyncio.sleep(1.0 * (attempt + 1))
                return await do_get(session, url, attempt + 1)
            log.warning("GET %s failed: HTTP %d", url, r.status)
            return None
    except Exception as e:
        log.warning("GET %s exception: %s", url, e)
        if attempt < MAX_RETRIES:
            await asyncio.sleep(0.5 * (attempt + 1))
            return await do_get(session, url, attempt + 1)
        return None


async def do_post(session: aiohttp.ClientSession, form: dict, attempt: int = 0) -> str | None:
    body = urllib.parse.urlencode(form, encoding="utf-8")
    try:
        async with session.post(
            LIST_URL, data=body,
            headers=_ajax_headers(len(body.encode())),
            timeout=aiohttp.ClientTimeout(total=PAGE_TIMEOUT)
        ) as r:
            log.debug("  POST → %d (body len %d)", r.status, len(body))
            if r.status == 200:
                return await r.text()
            if r.status in (429, 502, 503) and attempt < MAX_RETRIES:
                await asyncio.sleep(1.0 * (attempt + 1))
                return await do_post(session, form, attempt + 1)
            log.warning("POST failed: HTTP %d", r.status)
            return None
    except Exception as e:
        log.warning("POST exception: %s", e)
        if attempt < MAX_RETRIES:
            await asyncio.sleep(0.5 * (attempt + 1))
            return await do_post(session, form, attempt + 1)
        return None


# ── HTML SAVE ──────────────────────────────────────────────────────────────────
async def save_html(state: str, district: str, filename: str, content: str):
    folder = OUTPUT_DIR / _safe_name(state) / _safe_name(district)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{_safe_name(filename)}.html"
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    log.info("  💾 Saved %s", path)
    return path


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN TEST LOGIC  (sequential — easy to follow / debug)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_test(session: aiohttp.ClientSession):
    # ── Step 1: Load the page ──────────────────────────────────────────────────
    log.info("━" * 60)
    log.info("Step 1 — Loading BPCL outlet list page…")
    page_html = await do_get(session, LIST_URL)
    if not page_html:
        log.error("❌ Could not load page. Check network / URL.")
        return
    log.info("  ✓ Page loaded (%d bytes)", len(page_html))

    # Optional: save raw page for inspection
    await save_html("_debug", "_debug", "main_page", page_html)

    hidden = parse_hidden(page_html)
    if not hidden.get("__VIEWSTATE"):
        log.error("❌ No __VIEWSTATE found — page structure may have changed.")
        return

    # ── Step 2: Find target state ──────────────────────────────────────────────
    log.info("━" * 60)
    log.info("Step 2 — Finding state '%s'…", TARGET_STATE)
    state_opts = get_opts_from_page(page_html, "ddlState")
    if not state_opts:
        # Try alternate select IDs the site might use
        for sid in ("ctl00_ContentPlaceHolder1_ddlState", "State", "state"):
            state_opts = get_opts_from_page(page_html, sid)
            if state_opts:
                log.info("  Found states via alternate ID: %s", sid)
                break

    if not state_opts:
        log.error("❌ No states found in page. Saving page for inspection…")
        log.error("   Check test_html/_debug/_debug/main_page.html")
        return
    log.info("  ✓ Total states found: %d", len(state_opts))

    target = next(
        (s for s in state_opts
         if TARGET_STATE.lower() in s["text"].lower()),
        None
    )
    if not target:
        log.error("❌ State '%s' not found. Available states:", TARGET_STATE)
        for s in state_opts:
            log.error("    %s", s["text"])
        return
    log.info("  ✓ Target state: %s (value=%s)", target["text"], target["value"])

    # ── Step 3: POST state → get districts ────────────────────────────────────
    log.info("━" * 60)
    log.info("Step 3 — Posting state selection…")
    await asyncio.sleep(DELAY_S)
    r1 = await do_post(session, mk_form(hidden, "ddlState",
                                        {"ddlState": target["value"]}))
    if not r1:
        log.error("❌ POST for state failed.")
        return

    await save_html("_debug", "_debug", "state_response", r1)
    panels1, hidden1 = parse_delta(r1)
    hidden.update(hidden1)

    districts = get_opts(panels1, "ddlDistrict")
    if not districts:
        log.error("❌ No districts found in delta response.")
        log.error("   Response preview: %s", r1[:500])
        return
    log.info("  ✓ Districts found: %d — %s",
             len(districts), [d["text"] for d in districts])

    # ── Step 4: Process each district ─────────────────────────────────────────
    total_saved = 0

    for dist_idx, dist in enumerate(districts):
        log.info("━" * 60)
        log.info("District %d/%d: %s", dist_idx + 1, len(districts), dist["text"])

        # FIX 4: Refresh hidden fields fresh for every district
        fresh_page = await do_get(session, LIST_URL)
        if not fresh_page:
            log.warning("  ⚠ Could not refresh page for district %s — skipping", dist["text"])
            continue
        h = parse_hidden(fresh_page)

        # Re-select the state
        await asyncio.sleep(DELAY_S)
        r_state = await do_post(session, mk_form(h, "ddlState",
                                                  {"ddlState": target["value"]}))
        if not r_state:
            log.warning("  ⚠ State re-select failed for district %s", dist["text"])
            continue
        _, hs = parse_delta(r_state); h.update(hs)

        # Select the district
        await asyncio.sleep(DELAY_S)
        r2 = await do_post(session, mk_form(h, "ddlDistrict", {
            "ddlState"   : target["value"],
            "ddlDistrict": dist["value"],
        }))
        if not r2:
            log.warning("  ⚠ District POST failed: %s", dist["text"])
            continue

        await save_html("_debug", dist["text"], "district_response", r2)
        panels2, hidden2 = parse_delta(r2)
        h.update(hidden2)

        pins = get_opts(panels2, "ddlPinCode")
        if not pins:
            log.info("  No pincodes for %s — using district-level submit", dist["text"])
            pins = [{"value": "", "text": "_no_pin"}]

        log.info("  Pincodes: %d — %s",
                 len(pins), [p["text"] for p in pins[:10]])

        # ── Step 5: For each pincode, submit and save ──────────────────────────
        for pin_idx, pin in enumerate(pins):
            log.info("  Pin %d/%d: %s", pin_idx + 1, len(pins), pin["text"])

            # FIX: use a copy of h so we don't pollute across pins
            hp = dict(h)

            # Select pincode (if real)
            if pin["value"] and pin["value"] != "--Select Pin--":
                await asyncio.sleep(DELAY_S)
                r3 = await do_post(session, mk_form(hp, "ddlPinCode", {
                    "ddlState"   : target["value"],
                    "ddlDistrict": dist["value"],
                    "ddlPinCode" : pin["value"],
                }))
                if r3:
                    _, h3 = parse_delta(r3); hp.update(h3)

            # Submit the form
            await asyncio.sleep(DELAY_S)
            submit_form = dict(hp)
            submit_form.update({
                "__EVENTTARGET"  : "",
                "__EVENTARGUMENT": "",
                "ddlState"       : target["value"],
                "ddlDistrict"    : dist["value"],
                "ddlPinCode"     : pin["value"],
                "btnCheck"       : "Submit",
                SM_FIELD         : f"ctl00$ContentPlaceHolder1$ScriptManager1|btnCheck",
            })
            r4 = await do_post(session, submit_form)
            if r4:
                await save_html(target["text"], dist["text"],
                                f"{pin['text']}_submit", r4)
                total_saved += 1

            # GET the view page
            await asyncio.sleep(DELAY_S)
            view = await do_get(session, VIEW_URL)
            if view:
                await save_html(target["text"], dist["text"], pin["text"], view)
                total_saved += 1

                # Quick check: does the view page have outlet data?
                if "No Record" in view or "no record" in view:
                    log.info("    → No records for this pincode (page says 'No Record')")
                elif len(view) > 5000:
                    log.info("    ✓ View page looks populated (%d bytes)", len(view))
                else:
                    log.info("    ⚠ View page seems short (%d bytes)", len(view))

    # ── Done ──────────────────────────────────────────────────────────────────
    return total_saved


async def main():
    t0 = time.time()

    log.info("╔══════════════════════════════════════════════╗")
    log.info("║   BPCL Scraper — TEST MODE (single state)    ║")
    log.info("║   Target state : %-26s ║", TARGET_STATE)
    log.info("║   Output folder: %-26s ║", str(OUTPUT_DIR))
    log.info("╚══════════════════════════════════════════════╝")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    connector = aiohttp.TCPConnector(ssl=False, limit=5, ttl_dns_cache=300)
    cookie_jar = aiohttp.CookieJar()

    async with aiohttp.ClientSession(
        connector=connector,
        cookie_jar=cookie_jar,
        connector_owner=True,
        headers={"User-Agent": UA},
    ) as session:
        total = await run_test(session)

    elapsed = time.time() - t0
    log.info("━" * 60)
    if total:
        log.info("✅ TEST PASSED — %d HTML files saved in %.1fs", total, elapsed)
        log.info("   Inspect: %s", OUTPUT_DIR.resolve())
        log.info("   If results look correct, run the full-version script next.")
    else:
        log.info("❌ TEST FAILED — 0 files saved in %.1fs", elapsed)
        log.info("   Check test_html/_debug/_debug/main_page.html")
        log.info("   to see if the page structure has changed.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())