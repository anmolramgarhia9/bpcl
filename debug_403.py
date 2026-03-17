import asyncio
import aiohttp
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("test")

URL = "https://www.bharatpetroleum.in/our-businesses/fuels-and-services/retail-outlet-list"

UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"
]

async def test_header(session, ua, extra_headers=None):
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if extra_headers:
        headers.update(extra_headers)
    
    try:
        async with session.get(URL, headers=headers, timeout=10) as r:
            log.info(f"UA: {ua[:30]}... -> Status: {r.status}")
            return r.status
    except Exception as e:
        log.error(f"Error with UA {ua[:30]}: {e}")
        return None

async def main():
    async with aiohttp.ClientSession() as session:
        for ua in UAS:
            await test_header(session, ua)
            await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
