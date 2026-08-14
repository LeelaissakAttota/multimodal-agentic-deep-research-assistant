import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

OUT = Path('screenshots')
OUT.mkdir(exist_ok=True)

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        # Swagger UI
        await page.goto('http://127.0.0.1:8001/docs')
        await page.wait_for_timeout(1000)
        await page.screenshot(path=str(OUT / 'swagger-ui.png'), full_page=True)

        # POST response page
        post_html = (OUT / 'post_response.html').resolve().as_uri()
        await page.goto(post_html)
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(OUT / 'post_response.png'), full_page=True)

        # GET response page
        get_html = (OUT / 'get_response.html').resolve().as_uri()
        await page.goto(get_html)
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(OUT / 'get_response.png'), full_page=True)

        # Validation error page (empty objective)
        err_html = (OUT / 'validation_error.html').resolve().as_uri()
        if Path(err_html.replace('file://','')).exists():
            await page.goto(err_html)
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(OUT / 'validation_error.png'), full_page=True)

        await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
