"""
Этап 1 — проверка реализуемости.

Скрипт делает ОДИН запрос к странице поиска Авито (или читает страницу,
сохранённую вами из браузера), пытается извлечь объявления и показывает,
какие данные реально доступны. Ничего не обходит: если Авито отвечает
блокировкой или CAPTCHA, скрипт просто сообщает об этом и останавливается.

Запуск:
    python avito_probe.py "https://www.avito.ru/moskva/telefony/...?q=iphone+15"
    python avito_probe.py --file saved_page.html

Результат:
    probe_output/report.txt   — сводка: что найдено, статистика цен
    probe_output/items.csv    — таблица объявлений (открывается в Excel)
    probe_output/page.html    — сырая страница (для диагностики парсера)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://www.avito.ru"
OUT_DIR = Path("probe_output")

# Обычный браузерный заголовок, один и тот же при каждом запуске.
# Никакой ротации, прокси или имитации поведения — только один честный запрос.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}

BLOCK_MARKERS = (
    "доступ ограничен",
    "проблема с ip",
    "captcha",
    "firewall",
    "подтвердите, что вы не робот",
)


@dataclass
class Item:
    item_id: str = ""
    title: str = ""
    price: int | None = None
    url: str = ""
    location: str = ""
    date_text: str = ""
    description: str = ""
    photo: str = ""
    delivery: str = ""


def fetch(url: str) -> tuple[int, str]:
    import requests

    resp = requests.get(url, headers=HEADERS, timeout=30)
    return resp.status_code, resp.text


def detect_block(status: int, html: str) -> str | None:
    if status in (403, 429):
        return f"HTTP {status}: Авито ограничил доступ для этого IP/клиента."
    if status >= 400:
        return f"HTTP {status}: страница не получена."
    head = html[:20000].lower()
    for marker in BLOCK_MARKERS:
        if marker in head:
            return f"На странице признак блокировки/проверки: «{marker}»."
    return None


def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _price(raw: str | None) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def parse_html(html: str) -> list[Item]:
    """Разбор карточек выдачи по разметке data-marker / itemprop.

    Вёрстка Авито меняется; если карточки не найдены, report.txt подскажет,
    а page.html позволит поправить селекторы.
    """
    soup = BeautifulSoup(html, "lxml")
    items: list[Item] = []
    seen: set[str] = set()

    for card in soup.select('[data-marker="item"]'):
        item_id = card.get("data-item-id") or card.get("id", "").lstrip("i")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)

        title_link = card.select_one('[data-marker="item-title"]') or card.select_one(
            'a[itemprop="url"]'
        )
        price_meta = card.select_one('meta[itemprop="price"]')
        price_node = card.select_one('[data-marker="item-price"]')
        descr_meta = card.select_one('meta[itemprop="description"]')
        img = card.select_one("img")

        items.append(
            Item(
                item_id=str(item_id),
                title=title_link.get("title") or _text(title_link) if title_link else "",
                price=_price(price_meta.get("content") if price_meta else _text(price_node)),
                url=urljoin(BASE_URL, title_link.get("href", "")) if title_link else "",
                location=_text(
                    card.select_one('[data-marker="item-address"]')
                    or card.select_one('[data-marker="item-location"]')
                ),
                date_text=_text(card.select_one('[data-marker="item-date"]')),
                description=(descr_meta.get("content", "") if descr_meta else "")[:500],
                photo=(img.get("src") or "") if img else "",
                delivery=_text(card.select_one('[data-marker*="delivery"]')),
            )
        )
    return items


def price_stats(prices: list[int]) -> dict[str, float]:
    prices = sorted(prices)
    n = len(prices)
    if n == 0:
        return {}
    result: dict[str, float] = {
        "count": n,
        "min": prices[0],
        "max": prices[-1],
        "mean": round(statistics.fmean(prices)),
        "median": statistics.median(prices),
    }
    if n >= 4:
        q1, _, q3 = statistics.quantiles(prices, n=4, method="inclusive")
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        clean = [p for p in prices if lo <= p <= hi]
        result.update(
            q1=q1,
            q3=q3,
            outliers=n - len(clean),
            clean_median=statistics.median(clean),
        )
    return result


def coverage(items: list[Item]) -> dict[str, str]:
    """Какая доля карточек содержит каждое поле."""
    total = len(items) or 1
    out = {}
    for f in fields(Item):
        filled = sum(1 for it in items if getattr(it, f.name) not in ("", None))
        out[f.name] = f"{filled}/{len(items)} ({filled * 100 // total}%)"
    return out


def write_outputs(items: list[Item], html: str, source: str, status: int | None) -> str:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "page.html").write_text(html, encoding="utf-8")

    with open(OUT_DIR / "items.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[f.name for f in fields(Item)], delimiter=";")
        writer.writeheader()
        for it in items:
            writer.writerow(asdict(it))

    stats = price_stats([it.price for it in items if it.price])
    lines = [
        f"Источник: {source}",
        f"HTTP-статус: {status if status is not None else 'файл'}",
        f"Размер страницы: {len(html):,} символов",
        f"Найдено объявлений: {len(items)}",
        "",
        "Заполненность полей:",
        *[f"  {k:12} {v}" for k, v in coverage(items).items()],
        "",
        "Статистика цен (₽):",
        json.dumps(stats, ensure_ascii=False, indent=2) if stats else "  нет цен",
    ]
    if not items:
        lines += [
            "",
            "Карточки не распознаны. Возможные причины: изменилась вёрстка,",
            "страница-заглушка или пустая выдача. Пришлите probe_output/page.html",
            "(или его начало) — поправлю парсер.",
        ]
    report = "\n".join(lines)
    (OUT_DIR / "report.txt").write_text(report, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка доступности данных Авито")
    parser.add_argument("url", nargs="?", help="ссылка на страницу поиска Авито")
    parser.add_argument("--file", help="HTML-страница, сохранённая из браузера")
    args = parser.parse_args()

    if args.file:
        html = Path(args.file).read_text(encoding="utf-8", errors="replace")
        status, source = None, args.file
    elif args.url:
        if "avito.ru" not in args.url:
            print("Это не ссылка на avito.ru")
            return 2
        try:
            status, html = fetch(args.url)
        except Exception as exc:  # сеть, DNS, таймаут
            print(f"Не удалось выполнить запрос: {exc}")
            return 1
        source = args.url
        block = detect_block(status, html)
        if block:
            OUT_DIR.mkdir(exist_ok=True)
            (OUT_DIR / "page.html").write_text(html, encoding="utf-8")
            print(f"СТОП. {block}")
            print("Повторять запрос и обходить защиту скрипт не будет.")
            print("Попробуйте вариант --file со страницей, сохранённой из браузера.")
            return 3
    else:
        parser.print_help()
        return 2

    items = parse_html(html)
    print(write_outputs(items, html, source, status))
    print(f"\nФайлы сохранены в папку: {OUT_DIR.resolve()}")
    return 0 if items else 4


if __name__ == "__main__":
    sys.exit(main())
