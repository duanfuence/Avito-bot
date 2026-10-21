"""
Этап 1 — проверка реализуемости.

Скрипт проверяет, какие данные Авито доступны с вашего компьютера. Два режима:

  1. Ссылка на поиск Авито — ОДИН запрос к этой странице (картинки и
     карточки объявлений не загружаются). Повторный запрос по ссылке
     разрешён не раньше чем через 5 минут.
  2. Страница, сохранённая из браузера (.html) — без обращения к Авито.

Скрипт ничего не обходит: при блокировке, CAPTCHA, требовании входа или
ошибке доступа он останавливается и сообщает об этом. Cookies, логины и
пароли не используются и не отправляются.

Запуск без параметров (скрипт сам спросит ссылку или файл):
    python avito_probe.py
Или сразу:
    python avito_probe.py "https://www.avito.ru/..."
    python avito_probe.py --file "C:\\путь\\к\\странице.html"

Результаты — в папке probe_output рядом с этим скриптом:
    report.txt        сводка: что найдено, заполненность полей, статистика цен
    items.csv         таблица объявлений (открывается в Excel)
    page_sample.html  очищенная копия страницы для диагностики парсера
                      (без скриптов, форм, шапки сайта, e-mail и телефонов)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Консоль Windows может не поддерживать символ ₽ и кириллицу при перенаправлении вывода.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print(
        "ОШИБКА: не установлены нужные библиотеки.\n"
        "Запустите start_windows.bat — он установит их сам,\n"
        "или выполните: python -m pip install -r requirements.txt"
    )
    sys.exit(10)

BASE_URL = "https://www.avito.ru"
SCRIPT_DIR = Path(__file__).resolve().parent
OUT_DIR = SCRIPT_DIR / "probe_output"
LAST_RUN_FILE = OUT_DIR / ".last_live_request"
MIN_INTERVAL_SEC = 5 * 60
MAX_REDIRECTS = 3
TIMEOUT_SEC = 30

# Обычный заголовок браузера, всегда один и тот же. Без cookies, прокси,
# ротации IP или имитации действий пользователя.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

BLOCK_MARKERS = (
    "доступ ограничен",
    "доступ временно ограничен",
    "проблема с ip",
    "подтвердите, что вы не робот",
    "вы не робот",
    "captcha",
    "firewall",
)
LOGIN_PATH_MARKERS = ("/login", "/auth", "/profile/login")

# Параметры ссылки, которые не нужны в отчёте (служебные и отслеживающие).
DROP_QUERY_PARAMS = {"context", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"}

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Атрибуты, в которых лежит текст, написанный продавцом.
TEXT_ATTRS = {"content", "title", "alt", "aria-label", "value", "placeholder"}
PHONE_RE = re.compile(r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b")

# Признаки, по которым можно понять, как устроена страница (для диагностики).
STRUCTURE_MARKERS = (
    'data-marker="item"',
    'itemprop="price"',
    "__initialData__",
    "data-mfe-state",
    "__preloadedState__",
)


class ProbeStop(Exception):
    """Остановка с понятным сообщением для пользователя."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


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


# ---------------------------------------------------------------- безопасность

def mask_personal(text: str) -> str:
    text = EMAIL_RE.sub("[e-mail скрыт]", text)
    return PHONE_RE.sub("[телефон скрыт]", text)


def clean_url(url: str) -> str:
    """Ссылка без служебных параметров и якоря — для отчёта."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in DROP_QUERY_PARAMS and len(v) <= 100
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def sanitize_html(html: str) -> str:
    """Очищенная копия страницы: только видимая разметка, без личных данных.

    Удаляются скрипты (в них могут быть токены и данные аккаунта), стили,
    формы и поля ввода, шапка сайта с профилем, служебные meta-теги;
    e-mail и телефоны маскируются.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "form", "input",
                     "textarea", "header", "svg", "link", "template"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"data-marker": re.compile(r"header|profile|user|login", re.I)}):
        tag.decompose()
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if any(word in name for word in ("token", "csrf", "user", "session")):
            meta.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            value = tag.attrs[attr]
            if attr.startswith("on") or attr in ("nonce", "integrity"):
                del tag.attrs[attr]
            elif isinstance(value, str) and value.lower().startswith(("mailto:", "tel:")):
                tag.attrs[attr] = "[скрыто]"
            elif attr in TEXT_ATTRS and isinstance(value, str):
                tag.attrs[attr] = mask_personal(value)
            elif attr == "href" and isinstance(value, str) and "?" in value:
                tag.attrs[attr] = clean_url(value)
            elif isinstance(value, str) and EMAIL_RE.search(value):
                tag.attrs[attr] = EMAIL_RE.sub("[e-mail скрыт]", value)
    # Телефоны маскируются в тексте и текстовых атрибутах, но не в data-*/src:
    # там длинные числа — это ID объявлений и адреса картинок для диагностики.
    for node in soup.find_all(string=True):
        masked = mask_personal(str(node))
        if masked != node:
            node.replace_with(masked)
    return str(soup)


# ---------------------------------------------------------------- получение страницы

def check_rate_limit(now: float | None = None) -> None:
    now = time.time() if now is None else now
    try:
        last = float(LAST_RUN_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    wait = MIN_INTERVAL_SEC - (now - last)
    if wait > 0:
        raise ProbeStop(
            f"Запрос по ссылке уже был недавно. Подождите ещё {int(wait // 60) + 1} мин.\n"
            "Это ограничение защищает от лишней нагрузки на Авито и от блокировки.",
            5,
        )


def remember_live_request(now: float | None = None) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    LAST_RUN_FILE.write_text(str(time.time() if now is None else now), encoding="utf-8")


def validate_url(url: str) -> str:
    url = url.strip().strip('"').strip("'")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "avito.ru" or host.endswith(".avito.ru")):
        raise ProbeStop(
            "Это не ссылка на avito.ru.\n"
            "Скопируйте адрес из адресной строки браузера на странице поиска Авито.",
            2,
        )
    return url


def fetch(url: str) -> tuple[int, str, str, int]:
    """Один GET-запрос. Возвращает (статус, html, итоговый адрес, число HTTP-запросов)."""
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
    except requests.exceptions.SSLError:
        raise ProbeStop(
            "Ошибка защищённого соединения (SSL). Частые причины: антивирус проверяет\n"
            "HTTPS-трафик, неверные дата и время на компьютере, корпоративная сеть.",
            11,
        )
    except requests.exceptions.TooManyRedirects:
        raise ProbeStop("Слишком много перенаправлений — Авито не отдал страницу поиска.", 12)
    except requests.exceptions.Timeout:
        raise ProbeStop(f"Авито не ответил за {TIMEOUT_SEC} секунд. Проверьте интернет.", 13)
    except requests.exceptions.ConnectionError:
        raise ProbeStop("Нет соединения с avito.ru. Проверьте интернет, VPN или прокси.", 14)
    finally:
        session.close()

    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = "utf-8"
    return resp.status_code, resp.text, resp.url, len(resp.history) + 1


def detect_block(status: int, html: str, final_url: str, cards_found: int) -> str | None:
    """Понятное описание блокировки/ошибки или None, если страница в порядке."""
    if status in (401, 403, 429):
        return f"HTTP {status}: Авито ограничил доступ для вашего IP или браузера."
    if status >= 400:
        return f"HTTP {status}: Авито вернул ошибку, страница не получена."
    path = urlsplit(final_url).path.lower()
    if any(m in path for m in LOGIN_PATH_MARKERS):
        return "Авито перенаправил на страницу входа. Авторизация скриптом не выполняется."
    if cards_found:
        return None
    # Объявлений нет — проверяем, не заглушка ли это.
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    visible = soup.get_text(" ", strip=True).lower()
    for marker in BLOCK_MARKERS:
        if marker in visible:
            return f"Страница проверки или блокировки: найдено «{marker}»."
    return None


# ---------------------------------------------------------------- разбор

def _text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _price(raw: str | None) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def parse_html(html: str) -> list[Item]:
    """Карточки выдачи по разметке data-marker / itemprop.

    Вёрстка Авито может измениться; тогда report.txt и page_sample.html
    помогут поправить селекторы.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    seen: set[str] = set()

    for card in soup.select('[data-marker="item"]'):
        item_id = card.get("data-item-id") or card.get("id", "").lstrip("i")
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)

        title_link = card.select_one('[data-marker="item-title"]') or card.select_one('a[itemprop="url"]')
        price_meta = card.select_one('meta[itemprop="price"]')
        price_node = card.select_one('[data-marker="item-price"]')
        descr_meta = card.select_one('meta[itemprop="description"]')
        img = card.select_one("img")

        if title_link:
            title = title_link.get("title") or _text(title_link)
            url = urljoin(BASE_URL, title_link.get("href", ""))
        else:
            title = url = ""

        items.append(
            Item(
                item_id=str(item_id),
                title=title,
                price=_price(price_meta.get("content") if price_meta else _text(price_node)),
                url=clean_url(url) if url else "",
                location=_text(
                    card.select_one('[data-marker="item-address"]')
                    or card.select_one('[data-marker="item-location"]')
                ),
                date_text=_text(card.select_one('[data-marker="item-date"]')),
                description=mask_personal(descr_meta.get("content", "") if descr_meta else "")[:500],
                photo=(img.get("src") or "") if img else "",
                delivery=_text(card.select_one('[data-marker*="delivery"]')),
            )
        )
    return items


# ---------------------------------------------------------------- статистика и отчёт

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
        result.update(q1=q1, q3=q3, outliers=n - len(clean), clean_median=statistics.median(clean))
    return result


STAT_LABELS = {
    "count": "Объявлений с ценой",
    "min": "Минимум",
    "max": "Максимум",
    "mean": "Среднее",
    "median": "Медиана",
    "q1": "Первый квартиль (Q1)",
    "q3": "Третий квартиль (Q3)",
    "outliers": "Выбросов (вне Q1−1.5·IQR … Q3+1.5·IQR)",
    "clean_median": "Медиана без выбросов",
}


def coverage(items: list[Item]) -> list[str]:
    total = len(items)
    lines = []
    for f in fields(Item):
        filled = sum(1 for it in items if getattr(it, f.name) not in ("", None))
        pct = filled * 100 // total if total else 0
        lines.append(f"  {f.name:12} {filled}/{total} ({pct}%)")
    return lines


def write_outputs(items: list[Item], html: str, meta: dict[str, str]) -> str:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "page_sample.html").write_text(sanitize_html(html), encoding="utf-8")

    # utf-8-sig и разделитель «;» — чтобы русский Excel открыл файл двойным щелчком.
    with open(OUT_DIR / "items.csv", "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=[f.name for f in fields(Item)], delimiter=";")
        writer.writeheader()
        for it in items:
            writer.writerow(asdict(it))

    stats = price_stats([it.price for it in items if it.price])
    structure = [f"  {m:22} {html.count(m)}" for m in STRUCTURE_MARKERS]
    lines = [
        "Avito Market Scanner — пробный запуск",
        f"Время: {datetime.now():%Y-%m-%d %H:%M:%S}",
        *[f"{k}: {v}" for k, v in meta.items()],
        f"Размер страницы: {len(html):,} символов".replace(",", " "),
        f"Найдено объявлений: {len(items)}",
        "",
        "Заполненность полей:",
        *coverage(items),
        "",
        "Статистика цен (₽):",
        *([f"  {STAT_LABELS.get(k, k)}: {v:,.0f}".replace(",", " ") for k, v in stats.items()] or ["  нет цен"]),
        "",
        "Структура страницы (для диагностики):",
        *structure,
    ]
    if not items:
        lines += [
            "",
            "Объявления не распознаны. Возможные причины: изменилась вёрстка Авито,",
            "пустая выдача или страница-заглушка. Пришлите этот отчёт и, если",
            "попросят, файл page_sample.html (личные данные из него удалены).",
        ]
    report = "\n".join(lines)
    (OUT_DIR / "report.txt").write_text(report + "\n", encoding="utf-8")
    (OUT_DIR / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


# ---------------------------------------------------------------- запуск

def read_file(path_str: str) -> str:
    path = Path(path_str.strip().strip('"').strip("'"))
    if not path.is_file():
        raise ProbeStop(f"Файл не найден: {path}\nПроверьте путь или перетащите файл в окно мышкой.", 6)
    if path.suffix.lower() not in (".html", ".htm", ".mhtml", ".mht"):
        raise ProbeStop("Нужен файл страницы с расширением .html (Ctrl+S в браузере).", 7)
    if path.suffix.lower() in (".mhtml", ".mht"):
        raise ProbeStop(
            "Файл сохранён в формате MHTML. Сохраните страницу заново:\n"
            "Ctrl+S → тип файла «Веб-страница полностью» или «Только HTML».",
            7,
        )
    return path.read_text(encoding="utf-8", errors="replace")


def ask_source() -> tuple[str | None, str | None]:
    print("Что проверяем?")
    print("  • Вставьте ссылку на страницу поиска Авито (правая кнопка мыши или Ctrl+V)")
    print("  • или перетащите мышкой в это окно сохранённый файл .html")
    print()
    answer = input("Ссылка или файл, затем Enter: ").strip()
    # PowerShell при перетаскивании файла может добавить «& » перед путём.
    answer = answer.removeprefix("& ").strip().strip('"').strip("'")
    if not answer:
        raise ProbeStop("Ничего не введено.", 2)
    if answer.lower().startswith(("http://", "https://", "www.", "avito.ru")):
        if not answer.lower().startswith("http"):
            answer = "https://" + answer
        return answer, None
    return None, answer


def run(url: str | None, file: str | None) -> int:
    if file:
        html = read_file(file)
        meta = {"Режим": "файл из браузера (запросов к Авито не было)", "Файл": Path(file.strip('"')).name}
    else:
        url = validate_url(url or "")
        check_rate_limit()
        print("\nВыполняю один запрос к Авито…")
        remember_live_request()
        status, html, final_url, http_requests = fetch(url)
        meta = {
            "Режим": "один запрос по ссылке",
            "Ссылка": clean_url(url),
            "HTTP-статус": str(status),
            "HTTP-запросов (с перенаправлениями)": str(http_requests),
        }
        block = detect_block(status, html, final_url, len(parse_html(html)))
        if block:
            OUT_DIR.mkdir(exist_ok=True)
            (OUT_DIR / "page_sample.html").write_text(sanitize_html(html), encoding="utf-8")
            report = "\n".join(
                ["Avito Market Scanner — пробный запуск", f"Время: {datetime.now():%Y-%m-%d %H:%M:%S}",
                 *[f"{k}: {v}" for k, v in meta.items()], "", f"ОСТАНОВЛЕНО: {block}"]
            )
            (OUT_DIR / "report.txt").write_text(report + "\n", encoding="utf-8")
            raise ProbeStop(
                f"{block}\nСкрипт остановлен и не будет повторять запрос или обходить ограничение.\n"
                "Это тоже полезный результат теста. Попробуйте режим с сохранённой страницей:\n"
                "откройте поиск в браузере, нажмите Ctrl+S и передайте файл скрипту.",
                3,
            )

    items = parse_html(html)
    print()
    print(write_outputs(items, html, meta))
    print()
    print("Готово. Файлы результатов:")
    for name in ("report.txt", "items.csv"):
        print(f"  {OUT_DIR / name}")
    return 0 if items else 4


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка доступности данных Авито")
    parser.add_argument("url", nargs="?", help="ссылка на страницу поиска Авито")
    parser.add_argument("--file", help="страница поиска, сохранённая из браузера (.html)")
    args = parser.parse_args()

    try:
        url, file = (args.url, args.file) if (args.url or args.file) else ask_source()
        return run(url, file)
    except ProbeStop as stop:
        print(f"\nСТОП. {stop}")
        return stop.code
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.")
        return 130
    except Exception as exc:  # непредвиденная ошибка — без трассировки, но с типом
        print(f"\nНепредвиденная ошибка: {type(exc).__name__}: {exc}")
        print("Скопируйте это сообщение и пришлите разработчику.")
        return 1


if __name__ == "__main__":
    code = main()
    if os.name == "nt" and code in (0, 4) and OUT_DIR.exists():
        try:
            os.startfile(OUT_DIR)  # открыть папку с результатами в Проводнике
        except OSError:
            pass
    sys.exit(code)
