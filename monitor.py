# monitor.py
import os, re, time, math, asyncio, traceback, requests
from datetime import datetime
from typing import Optional, Dict, List
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --------- Config (via variables d'env Render) ----------
TARGET_URL   = os.getenv("TARGET_URL", "https://www.altered.gg/fr-fr/cards/market?order[price]=ASC&rarity[]=UNIQUE")
IFTTT_KEY    = os.getenv("IFTTT_KEY", "")
IFTTT_EVENT  = os.getenv("IFTTT_EVENT", "altered_min_price")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
USER_AGENT   = os.getenv("USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
STATE_PATH   = os.getenv("STORAGE_STATE_PATH", "/etc/secrets/storage_state.json")
REQUEST_TIMEOUT_MS = int(os.getenv("REQUEST_TIMEOUT_MS", "25000"))       # timeout par action
WAIT_BADGE_TIMEOUT_MS = int(os.getenv("WAIT_BADGE_TIMEOUT_MS", "15000")) # attente spécifique des badges/prix
MAX_GOTO_RETRIES = int(os.getenv("MAX_GOTO_RETRIES", "3"))
MAX_SCAN_ITEMS = int(os.getenv("MAX_SCAN_ITEMS", "200"))                 # garde-fou perfs
# -------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def send_ifttt(title: str, price: float, link: str):
    if not IFTTT_KEY:
        log("[IFTTT][WARN] IFTTT_KEY manquant — notif non envoyée.")
        return
    try:
        r = requests.post(
            f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/json/with/key/{IFTTT_KEY}",
            json={"value1": title, "value2": f"{price:.2f} €", "value3": link},
            timeout=15,
        )
        log(f"[IFTTT] {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"[IFTTT][ERR] {e}")

PRICE_RE = re.compile(r"À\s*PARTIR\s*DE\s*([0-9]+(?:[.,][0-9]{1,2})?)\s*€", re.I)

def parse_price_from_text(text: str) -> Optional[float]:
    # gère les espaces insécables / fines
    t = text.replace("\xa0", " ").replace("\u202f", " ")
    m = PRICE_RE.search(t)
    if not m:
        # fallback très permissif (dernière chance)
        m2 = re.search(r"(\d+(?:[.,]\d{1,2})?)\s*€", t)
        if not m2:
            return None
        raw = m2.group(1)
    else:
        raw = m.group(1)
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None

async def goto_with_retries(page, url: str):
    last_exc = None
    for i in range(1, MAX_GOTO_RETRIES + 1):
        try:
            log(f"[NAV] goto try {i}/{MAX_GOTO_RETRIES} → {url}")
            # domcontentloaded = plus fiable/rapide que networkidle pour des sites SPA
            await page.goto(url, timeout=REQUEST_TIMEOUT_MS, wait_until="domcontentloaded")
            # attendre la présence d’un badge/prix "À PARTIR DE" (n’importe où) pour s’assurer que la liste a rendu
            await page.wait_for_selector('text=/À\\s*PARTIR\\s*DE/i', timeout=WAIT_BADGE_TIMEOUT_MS)
            return True
        except PWTimeout as e:
            last_exc = e
            log(f"[NAV][WARN] Timeout goto/wait (try {i}) : {e}")
            await asyncio.sleep(2)
        except Exception as e:
            last_exc = e
            log(f"[NAV][WARN] Exception goto (try {i}) : {e}")
            await asyncio.sleep(2)
    log(f"[NAV][ERR] Échec de navigation après {MAX_GOTO_RETRIES} tentatives : {last_exc}")
    return False

async def find_min_price_card(page) -> Dict:
    """
    Récupère toutes les "cards" qui :
      - contiennent un bouton 'ACHETER'
      - contiennent un prix 'À PARTIR DE X €'
      - NE contiennent PAS 'Foiler' (strict, insensible à la casse)
    Retourne {count, min_price, min_card:{title, price, url}}
    """
    # conteneurs plausibles ; on commence large puis on filtre
    containers = page.locator("article, li, div").filter(
        has_text=re.compile(r"\bACHETER\b", re.I)
    ).filter(
        has_text=re.compile(r"À\s*PARTIR\s*DE", re.I)
    )

    total = await containers.count()
    log(f"[SCRAPE] Conteneurs (ACHETER + À PARTIR DE) : {total}")

    cards: List[Dict] = []
    scan_count = min(total, MAX_SCAN_ITEMS)
    for i in range(scan_count):
        item = containers.nth(i)

        # texte brut (1 seule fois pour limiter les appels)
        try:
            txt = await item.inner_text()
        except PWTimeout:
            continue

        # exclure Foiler STRICT (avant toute autre extraction)
        if re.search(r"\bFoiler\b", txt, re.I):
            continue

        # prix depuis le badge
        price = parse_price_from_text(txt)
        if price is None:
            # log minimal pour debug si besoin
            snippet = " | ".join([l.strip() for l in txt.splitlines() if l.strip()])[:160]
            log(f"[DEBUG] Prix non parsé (skip) : {snippet}")
            continue

        # titre : préférer un élément titre, sinon heuristique propre
        title = None
        try:
            title_el = item.locator("h3, h2, .title, [data-testid=card-title]").first
            if await title_el.count() > 0:
                t = await title_el.inner_text()
                title = t.strip()
        except Exception:
            pass
        if not title:
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            title = next(
                (l for l in lines
                 if "À PARTIR" not in l.upper()
                 and "ACHETER" not in l.upper()
                 and "VENDRE" not in l.upper()
                 and not re.search(r"\bFoiler\b", l, re.I)),
                "Carte unique"
            )

        # URL (fallback = TARGET_URL)
        url = TARGET_URL
        try:
            link = item.locator("a").first
            if await link.count() > 0:
                href = await link.get_attribute("href")
                if href:
                    if href.startswith("http"):
                        url = href
                    else:
                        from urllib.parse import urljoin
                        url = urljoin(TARGET_URL, href)
        except Exception:
            pass

        cards.append({"title": title, "price": price, "url": url})

    cards.sort(key=lambda x: x["price"])
    min_card = cards[0] if cards else None
    min_price = min_card["price"] if min_card else None

    return {
        "count": len(cards),
        "min_price": min_price,
        "min_card": min_card
    }

async def main():
    global best_seen_price, best_seen_title

    log("[BOOT] Surveillance Altered marketplace")

    if not os.path.exists(STATE_PATH):
        log(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}")
        log("             → Render > Environment > Secret Files : storage_state.json")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        # IMPORTANT : lecture seule, on ne sauvegarde jamais l'état → pas de risque d'écraser
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            storage_state=STATE_PATH
        )
        context.set_default_timeout(REQUEST_TIMEOUT_MS)
        page = await context.new_page()

        while True:
            try:
                log(f"[LOOP] {datetime.now().strftime('%H:%M:%S')} Chargement…")

                ok = await goto_with_retries(page, TARGET_URL)
                if not ok:
                    # on skip l’itération actuelle, on réessaie au prochain tour
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                # session expirée ?
                if page.url.startswith("https://auth.altered.gg"):
                    log("[AUTH][ERR] Session expirée / login requis. Regénère storage_state.json.")
                    await asyncio.sleep(max(POLL_SECONDS, 60))
                    continue

                # déclenche éventuellement du lazy-load
                try:
                    await page.evaluate("window.scrollTo(0, 600)")
                except Exception:
                    pass

                data = await find_min_price_card(page)
                cnt = data["count"]
                min_price = data["min_price"]
                min_card = data["min_card"]

                if cnt == 0 or min_price is None or not min_card:
                    log("[INFO] 0 carte unique détectée (Foiler exclus).")
                else:
                    log(f"[INFO] Cartes uniques détectées (Foiler exclus) : {cnt}")
                    log(f"[INFO] Min courant : {min_price:.2f} € — {min_card['title']} — {min_card['url']}")

                    # Alerte uniquement si nouveau plus bas strict
                    if (best_seen_price is math.inf) or (min_price < best_seen_price - 1e-9):
                        log(f"[ALERT] Nouveau plus bas {min_price:.2f} € (ancien {best_seen_price if best_seen_price < math.inf else '∞'})")
                        send_ifttt(min_card['title'] or "Carte unique", min_price, min_card['url'])
                        best_seen_price, best_seen_title = min_price, min_card['title']

            except PWTimeout:
                log("[WARN] Timeout d'action Playwright (goto / selector).")
            except Exception as e:
                log(f"[ERR] Boucle : {e}\n{traceback.format_exc()}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())



