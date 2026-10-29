import asyncio, json, statistics, sys
from playwright.async_api import async_playwright
EXE = "/opt/pw-browsers/chromium"
FAST4G = dict(offline=False, latency=165, downloadThroughput=int(9*1024*1024/8), uploadThroughput=int(1.5*1024*1024/8))

async def run(pw, port, w=1366, h=900, shot=None, dark=False):
    b = await pw.chromium.launch(executable_path=EXE) if False else await pw.chromium.launch()
    ctx = await b.new_context(viewport={"width": w, "height": h})
    await ctx.add_init_script("sessionStorage.setItem('t','t');sessionStorage.setItem('n','Test Student');sessionStorage.setItem('m','260002@astanait.edu.kz')")
    page = await ctx.new_page()
    cdp = await ctx.new_cdp_session(page)
    await cdp.send("Network.enable"); await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
    await cdp.send("Network.emulateNetworkConditions", FAST4G)
    reqs, sizes, urls = {}, {}, {}
    cdp.on("Network.requestWillBeSent", lambda e: urls.__setitem__(e["requestId"], e["request"]["url"]))
    cdp.on("Network.loadingFinished", lambda e: sizes.__setitem__(e["requestId"], e["encodedDataLength"]))
    errs = []
    page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errs.append(str(e)))
    await page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    await page.wait_for_selector(".cards .card:not(.sk)")
    await page.wait_for_timeout(600)
    t = await page.evaluate("(()=>{const n=performance.getEntriesByType('navigation')[0];return {load:n.loadEventEnd, dcl:n.domContentLoadedEventEnd}})()")
    sw = await page.evaluate("document.documentElement.scrollWidth")
    if dark: await page.evaluate("document.documentElement.dataset.theme='dark'")
    if shot: await page.screenshot(path=shot)
    await b.close()
    items = [(urls[k].split(f":{port}")[-1], v) for k, v in sizes.items() if k in urls]
    return dict(total_kB=sum(v for _, v in items)/1000, n=len(items), load_ms=t["load"], scrollWidth=sw, errs=errs, items=items)

async def main():
    async with async_playwright() as pw:
        name = sys.argv[1]; port = 5000
        runs = [await run(pw, port) for _ in range(5)]
        out = dict(variant=name, total_kB=statistics.median(r["total_kB"] for r in runs), n=runs[0]["n"],
                   load_ms=statistics.median(r["load_ms"] for r in runs), all_load=[round(r["load_ms"]) for r in runs],
                   all_kB=[round(r["total_kB"],1) for r in runs], items=runs[0]["items"], errs=runs[-1]["errs"])
        json.dump(out, open(f"result_{name}.json","w"), indent=1)
        r = await run(pw, port, w=360, h=800, shot=f"shot360_{name}.png")
        print(name, "360px scrollWidth =", r["scrollWidth"], "errors:", r["errs"])
        await run(pw, port, shot=f"shot_{name}_light.png"); await run(pw, port, shot=f"shot_{name}_dark.png", dark=True)

asyncio.run(main())
