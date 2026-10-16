"""
Опрос страниц электронной очереди ДП «Документ» через Playwright.
Форма на /solutions/e-queue: выбор услуги -> подгружаются дни -> подгружаются часы.
Мы только читаем опции выпадающих списков, ничего не отправляем.
"""
import asyncio
import logging
import random

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger("checker")

PLACEHOLDER = "обрати"  # "- Обрати -" — пустой пункт


def _is_real(label: str) -> bool:
    l = label.strip().lower()
    return bool(l) and PLACEHOLDER not in l and not l.startswith("-")


async def _options(page, selector: str) -> list[tuple[str, str]]:
    """Вернуть [(value, label)] реальных опций селекта."""
    return await page.eval_on_selector(
        selector,
        """el => Array.from(el.options).map(o => [o.value, o.textContent.trim()])""",
    )


async def check_center(context, subdomain: str, with_times: bool = True) -> dict[str, list[str]]:
    """
    Возвращает {день: [часы...]} для центра. Пустой dict = слотов нет.
    Если with_times=False — часы не раскрываем, значения будут [].
    """
    url = f"https://{subdomain}.pasport.org.ua/solutions/e-queue"
    page = await context.new_page()
    result: dict[str, list[str]] = {}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(1500)

        selects = page.locator("select")
        n = await selects.count()
        if n < 3:
            # Cloudflare challenge или изменилась вёрстка
            title = await page.title()
            raise RuntimeError(f"{subdomain}: найдено {n} select'ов, title={title!r}")

        service_sel, day_sel, time_sel = "select >> nth=0", "select >> nth=1", "select >> nth=2"

        services = [v for v, l in await _options(page, service_sel) if _is_real(l)]
        if not services:
            return result
        await page.select_option(service_sel, services[0])

        # ждём, пока подгрузятся дни (или станет ясно, что их нет)
        try:
            await page.wait_for_function(
                """() => { const s = document.querySelectorAll('select')[1];
                           return s && Array.from(s.options).some(o => o.value && !/обрати/i.test(o.textContent)); }""",
                timeout=8_000,
            )
        except PWTimeout:
            return result  # дней нет — слотов нет

        days = [(v, l) for v, l in await _options(page, day_sel) if _is_real(l)]
        for value, label in days:
            times: list[str] = []
            if with_times:
                try:
                    await page.select_option(day_sel, value)
                    await page.wait_for_function(
                        """() => { const s = document.querySelectorAll('select')[2];
                                   return s && Array.from(s.options).some(o => o.value && !/обрати/i.test(o.textContent)); }""",
                        timeout=6_000,
                    )
                    times = [l for v, l in await _options(page, time_sel) if _is_real(l)]
                except PWTimeout:
                    times = []
                await page.wait_for_timeout(random.randint(300, 900))
            result[label] = times
        return result
    finally:
        await page.close()


async def run_once(subdomains: list[str], with_times: bool = True) -> dict[str, dict[str, list[str]] | Exception]:
    """Проверить все центры в одном браузере. На ошибку центра — Exception в значении."""
    out: dict = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            locale="uk-UA",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        for sub in subdomains:
            try:
                out[sub] = await check_center(context, sub, with_times)
            except Exception as e:  # noqa: BLE001
                log.warning("check %s failed: %s", sub, e)
                out[sub] = e
            await asyncio.sleep(random.uniform(1.5, 4))
        await browser.close()
    return out


if __name__ == "__main__":
    import sys, json
    logging.basicConfig(level=logging.INFO)
    subs = sys.argv[1:] or ["varna"]
    res = asyncio.run(run_once(subs))
    print(json.dumps({k: (str(v) if isinstance(v, Exception) else v) for k, v in res.items()},
                     ensure_ascii=False, indent=2))
