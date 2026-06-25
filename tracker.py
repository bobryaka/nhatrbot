#!/usr/bin/env python3
"""
Мониторинг цены тура: Алматы → Нячанг
Вылет: 11.07.2026  Возврат: 19.07.2026  1 взрослый
Сайты: ht.kz и poedem.kz

Запуск:
    python tracker.py              — проверка, результат только в консоль
    python tracker.py --notify     — проверка + отправить в Telegram если цена упала
    python tracker.py --debug      — показать браузер (для ht.kz)
    python tracker.py --test-tg    — тестовое сообщение в Telegram

Cron (каждый час, с уведомлениями):
    0 * * * * cd /opt/tracker && venv/bin/python tracker.py --notify >> tracker.log 2>&1

Установка:
    pip install playwright requests
    playwright install chromium
"""

import argparse
import asyncio
import logging
import re
import sqlite3
import sys
from datetime import datetime
from typing import Optional

import requests
from playwright.async_api import async_playwright, Route

# ════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ — заполнить перед запуском
# ════════════════════════════════════════════════════════════════════

import os

TG_TOKEN   = os.environ.get("TG_TOKEN",   "YOUR_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "YOUR_CHAT_ID")

# Уведомлять если цена упала на ОБА условия одновременно
MIN_DROP_KZT     = 10_000  # тенге
MIN_DROP_PERCENT = 2.0     # процентов

DB_PATH = "prices.db"

# ════════════════════════════════════════════════════════════════════
#  Параметры поиска (под твой конкретный тур)
# ════════════════════════════════════════════════════════════════════

DEPART_DATE_HT = "11.7.2026"   # для ht.kz  (без нуля — их формат)
DEPART_DATE_PD = "11.07.2026"  # для poedem.kz
RETURN_DATE    = "19.07.2026"  # для информации в уведомлении
NIGHTS         = 8             # 11.07 → 19.07 = 8 ночей
ADULTS         = 1

# ════════════════════════════════════════════════════════════════════
#  Logging
# ════════════════════════════════════════════════════════════════════

log = logging.getLogger("tracker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%d.%m %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("tracker.log", encoding="utf-8"),
    ],
)

# ════════════════════════════════════════════════════════════════════
#  SQLite
# ════════════════════════════════════════════════════════════════════

def db_open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prices (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            site       TEXT    NOT NULL,
            price_kzt  REAL    NOT NULL,
            checked_at TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notified (
            site        TEXT PRIMARY KEY,
            price_kzt   REAL NOT NULL,
            notified_at TEXT NOT NULL
        );
    """)
    return conn

def db_save(conn, site: str, price: float):
    conn.execute(
        "INSERT INTO prices (site, price_kzt, checked_at) VALUES (?,?,?)",
        (site, price, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()

def db_min_ever(conn, site: str) -> Optional[float]:
    row = conn.execute(
        "SELECT MIN(price_kzt) FROM prices WHERE site=?", (site,)
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None

def db_last_notified(conn, site: str) -> Optional[float]:
    row = conn.execute(
        "SELECT price_kzt FROM notified WHERE site=?", (site,)
    ).fetchone()
    return float(row[0]) if row else None

def db_set_notified(conn, site: str, price: float):
    conn.execute(
        """INSERT INTO notified (site, price_kzt, notified_at) VALUES (?,?,?)
           ON CONFLICT(site) DO UPDATE
           SET price_kzt=excluded.price_kzt, notified_at=excluded.notified_at""",
        (site, price, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()

# ════════════════════════════════════════════════════════════════════
#  Telegram
# ════════════════════════════════════════════════════════════════════

def tg_send(text: str, notify: bool):
    """Отправить в Telegram (только если передан флаг --notify)."""
    if not notify:
        return
    if TG_TOKEN == "YOUR_BOT_TOKEN":
        log.warning("Telegram не настроен (поменяй TG_TOKEN / TG_CHAT_ID)")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        log.info("Telegram ✓")
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ════════════════════════════════════════════════════════════════════
#  Извлечение цен из JSON/HTML
# ════════════════════════════════════════════════════════════════════

# Ключи в JSON, которые могут содержать цену тура в KZT
_PRICE_KEYS = {"price", "pricefrom", "minprice", "cost", "amount", "totalprice", "sum"}

def _walk_json(obj, out: list, depth=0):
    if depth > 12:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in _PRICE_KEYS:
                if isinstance(v, (int, float)) and 50_000 <= v <= 15_000_000:
                    out.append(float(v))
                elif isinstance(v, str):
                    try:
                        n = float(v.replace(" ", "").replace(",", "."))
                        if 50_000 <= n <= 15_000_000:
                            out.append(n)
                    except ValueError:
                        pass
            _walk_json(v, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, out, depth + 1)

def _from_html(html: str) -> list:
    out = []
    for pat in [
        r'(\d{1,3}(?:\s\d{3})+)\s*[₸]',                          # "150 000 ₸"
        r'(\d{5,7})\s*(?:₸|тг\.?|KZT)',                           # "150000₸"
        r'"(?:price|minPrice|priceFrom|cost)"\s*:\s*"?(\d{5,7})',  # JSON в HTML
    ]:
        for m in re.finditer(pat, html):
            try:
                v = float(m.group(1).replace(" ", ""))
                if 50_000 <= v <= 15_000_000:
                    out.append(v)
            except ValueError:
                pass
    return out

# ════════════════════════════════════════════════════════════════════
#  Scraper: ht.kz
# ════════════════════════════════════════════════════════════════════

async def scrape_htkz(debug: bool) -> Optional[float]:
    """
    ht.kz — перехватываем ws.ht.kz/v1/search/web/{uuid} и меняем sort на price.
    Результат: минимальная цена из tours[].price.value.
    """
    SEARCH_URL = (
        "https://ht.kz/findtours"
        f"?region=63&departCity=1&country=7"
        f"&daysFrom=9&daysTo=9"          # 9 дней = 11.07 → 19.07 по их логике
        f"&adult={ADULTS}&child=0&childAges="
        f"&dateFrom={DEPART_DATE_HT}&delta=0"
        f"&stars=any&search=1&from=mainSearch"
    )

    prices: list[float] = []

    async def handle_route(route: Route):
        url = route.request.url
        new_url = re.sub(r'sort=[^&]+', 'sort=price', url)
        new_url = re.sub(r'size=\d+', 'size=200', new_url)
        await route.continue_(url=new_url)

    async def on_response(resp):
        if "ws.ht.kz/v1/search/web/" not in resp.url:
            return
        try:
            data = await resp.json()
            for tour in data.get("tours", []):
                v = tour.get("price", {}).get("value")
                if isinstance(v, (int, float)) and v >= 200_000:
                    prices.append(float(v))
                    log.debug(f"ht.kz: {tour.get('hotelName')} — {v:,} ₸")
        except Exception as e:
            log.debug(f"ht.kz json: {e}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not debug,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        # Перехватываем только ws.ht.kz — не тормозим всю страницу
        await page.route("**/ws.ht.kz/**", handle_route)
        page.on("response", on_response)

        try:
            log.info(f"ht.kz → {SEARCH_URL}")
            # domcontentloaded — не ждём networkidle, иначе таймаут из-за перехвата
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            # Ждём пока ws.ht.kz вернёт результаты
            await page.wait_for_timeout(15_000)

            if not prices:
                log.error("ht.kz: цены не найдены → debug_htkz.png")
                await page.screenshot(path="debug_htkz.png", full_page=True)
        except Exception as e:
            log.error(f"ht.kz: {e}", exc_info=True)
            try:
                await page.screenshot(path="debug_htkz_err.png")
            except Exception:
                pass
        finally:
            await browser.close()

    if prices:
        result = min(prices)
        log.info(f"ht.kz: мин. цена = {result:,.0f} ₸  (из {len(prices)} туров)")
        return result
    return None


# ════════════════════════════════════════════════════════════════════
#  Scraper: poedem.kz  (простой HTTP-запрос, без Playwright)
# ════════════════════════════════════════════════════════════════════

async def scrape_poedem(debug: bool) -> Optional[float]:
    """
    poedem.kz — JS рендерит результаты на клиенте, requests.get даёт пустой шаблон.
    Используем Playwright: ждём появления span.price-for-one-value и читаем из DOM.
    """
    SEARCH_URL = (
        "https://poedem.kz/findtours"
        f"?departCity=1&country=7&region=63"
        f"&dateFrom={DEPART_DATE_PD}&dateTo={DEPART_DATE_PD}"
        f"&nightsFrom={NIGHTS}&nightsTo={NIGHTS}"
        f"&adult={ADULTS}&child=0&ages=&stars=any&search=1"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not debug,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        )
        page = await ctx.new_page()

        prices: list[float] = []
        try:
            log.info(f"poedem.kz → {SEARCH_URL}")
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)

            # Ждём появления карточек с ценами (таймаут 30с)
            await page.wait_for_selector("span.price-for-one-value", timeout=30_000)

            # Читаем все цены из DOM
            elements = await page.query_selector_all("span.price-for-one-value")
            for el in elements:
                text = await el.inner_text()
                clean = re.sub(r'[^\d]', '', text)
                try:
                    v = float(clean)
                    if v >= 200_000:
                        prices.append(v)
                except ValueError:
                    pass

            if not prices:
                log.error("poedem.kz: span.price-for-one-value есть, но цены не распарсились")
                await page.screenshot(path="debug_poedem.png")

        except Exception as e:
            log.error(f"poedem.kz: {e}", exc_info=True)
            try:
                await page.screenshot(path="debug_poedem.png")
            except Exception:
                pass
        finally:
            await browser.close()

    if prices:
        result = min(prices)
        log.info(f"poedem.kz: мин. цена = {result:,.0f} ₸  (из {len(prices)} туров)")
        return result
    return None

# ════════════════════════════════════════════════════════════════════
#  Оркестратор
# ════════════════════════════════════════════════════════════════════

async def run(debug: bool, notify: bool):
    conn = db_open()

    # Запускаем оба скрапера параллельно
    raw = await asyncio.gather(
        scrape_htkz(debug),
        scrape_poedem(debug),
        return_exceptions=True,
    )

    results = [
        ("ht.kz",     raw[0] if not isinstance(raw[0], Exception) else None),
        ("poedem.kz", raw[1] if not isinstance(raw[1], Exception) else None),
    ]

    for site, price in results:
        if isinstance(price, Exception):
            log.error(f"{site}: исключение — {price}")
            continue
        if price is None:
            log.warning(f"{site}: цену получить не удалось")
            continue

        db_save(conn, site, price)

        # Сравниваем с последней ценой, о которой уже отправляли уведомление.
        # Если ни разу не уведомляли — берём исторический минимум.
        baseline = db_last_notified(conn, site) or db_min_ever(conn, site)

        if baseline is None:
            log.info(f"{site}: первая запись → {price:,.0f} ₸")
            continue

        drop     = baseline - price
        drop_pct = drop / baseline * 100

        log.info(
            f"{site}: {baseline:,.0f} → {price:,.0f} ₸  "
            f"(Δ {drop:+,.0f} ₸ / {drop_pct:+.1f}%)"
        )

        if drop >= MIN_DROP_KZT and drop_pct >= MIN_DROP_PERCENT:
            msg = (
                f"🔥 Цена снизилась!\n\n"
                f"Алматы → Нячанг (Вьетнам)\n"
                f"Вылет: {DEPART_DATE_PD}  ·  Возврат: {RETURN_DATE}\n"
                f"{NIGHTS} ночей  ·  {ADULTS} взр.\n"
                f"Источник: {site}\n\n"
                f"Было:  {baseline:,.0f} ₸\n"
                f"Стало: {price:,.0f} ₸\n"
                f"Скидка: −{drop:,.0f} ₸ ({drop_pct:.1f}%)\n\n"
                f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            print(f"\n{'='*50}\n{msg}\n{'='*50}\n")
            tg_send(
                msg.replace("🔥 Цена снизилась!", "🔥 <b>Цена снизилась!</b>")
                   .replace("Алматы → Нячанг", "Алматы → <b>Нячанг</b>")
                   .replace(f"Источник: {site}", f"Источник: <b>{site}</b>")
                   .replace(f"Стало: {price:,.0f} ₸", f"Стало: <b>{price:,.0f} ₸</b>")
                   .replace("Скидка:", "📉 Скидка:"),
                notify=notify,
            )
            db_set_notified(conn, site, price)
        else:
            log.info(f"{site}: ниже порога ({MIN_DROP_KZT:,} ₸ / {MIN_DROP_PERCENT}%), молчим")

    conn.close()

# ════════════════════════════════════════════════════════════════════
#  Entry point
# ════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Tour price tracker: Нячанг из Алматы")
    parser.add_argument("--debug",   action="store_true", help="Показать браузер (для отладки)")
    parser.add_argument("--notify",  action="store_true", help="Отправить в Telegram если цена упала")
    parser.add_argument("--test-tg", action="store_true", help="Тестовое сообщение в Telegram")
    args = parser.parse_args()

    if args.test_tg:
        tg_send(
            "✅ <b>Tour tracker подключён!</b>\n\n"
            f"Слежу за: Алматы → Нячанг\n"
            f"Вылет: {DEPART_DATE_PD}  •  Возврат: {RETURN_DATE}\n"
            f"Порог уведомления: −{MIN_DROP_KZT:,} ₸ / −{MIN_DROP_PERCENT}%",
            notify=True,
        )
        return

    if args.notify:
        log.info("Режим: проверка + уведомление в Telegram")
    else:
        log.info("Режим: только консоль (добавь --notify для Telegram)")

    asyncio.run(run(debug=args.debug, notify=args.notify))

if __name__ == "__main__":
    main()
