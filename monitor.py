# monitor.py
import os, re, math, asyncio, traceback, requests
from datetime import datetime
from typing import Optional, Dict
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
REQUEST_TIMEOUT_MS = int(os.getenv("REQUEST_TIMEOUT_MS", "25000"))
WAIT_BADGE_TIMEOUT_MS = int(os.getenv("WAIT_BADGE_TIMEOUT_MS", "15000"))
MAX_GOTO_RETRIES = int(os.getenv("MAX_GOTO_RETRIES", "3"))
MAX_SCAN_ITEMS = int(os.getenv("MAX_SCAN_ITEMS", "300"))  # garde-fou
# -------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

FOILER_RE = re.compile(r"\bfoiler\b", re.I)
PRICE_RE  = re.compile(r"À\s*PARTIR\s*DE\s*([0-9]+(?:[.,][0-9]{1,2})?)\s*€", re.I)

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

def parse_price(text: str) -> Optional[float]:
    t = text.replace("\xa0", " ").replace("\u202f", " ")
    m = PRICE_RE.search(t)
    if not m:
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

async def goto_with_retries(page, url: str) -> bool:
    last_exc = None
    for i in range(1, MAX_GOTO_RETRIES + 1):
        try:
            log(f"[NAV] goto try {i}/{MAX_GOTO_RETRIES} → {url}")
            await page.goto(url, timeout=REQUEST_TIMEOUT_MS, wait_until="domcontentloaded")
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

async def extract_title_and_price(item) -> Optional[Dict]:
    """
    Renvoie {title, price, url} pour un bloc; None si prix non parsé.
    Le titre est déduit d'éléments dédiés, sinon heuristique du texte.
    """
    try:
        txt = await item.inner_text()
    except PWTimeout:
        return None

    price = parse_price(txt)
    if price is None:
        return None

    # Titre
    title = None
    try:
        t_el = item.locator("h3, h2, .title, [data-testid=card-title]").first
        if await t_el.count() > 0:
            title = (await t_el.inner_text()).strip()
    except Exception:
        pass
    if not title:
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        title = next(
            (l for l in lines
             if "À PARTIR" not in l.upper()
             and "ACHETER" not in l.upper()
             and "VENDRE" not in l.upper()),
            "Carte unique"
        )

    # URL (si lien plausible trouvé, sinon liste)
    url = TARGET_URL
    try:
        links = item.locator("a")
        nlinks = await links.count()
        for k in range(nlinks):
            href = await links.nth(k).get_attribute("href")
            if not href:
                continue
            low = href.lower()
            # on prend un lien qui n'est pas "market" ni "foiler" si dispo
            if ("market" not in low) and ("foiler" not in low):
                if href.startswith("http"):
                    url = href
                else:
                    from urllib.parse import urljoin
                    url = urljoin(TARGET_URL, href)
                break
    except Exception:
        pass

    return {"title": title, "price": price, "url": url}

async def find_first_non_foiler(page) -> Optional[Dict]:
    """
    Parcourt la liste dans l'ordre et retourne la 1ʳᵉ carte dont le TITRE
    ne contient pas 'Foiler' (insensible à la casse). Ignore tout le reste.
    """
    containers = page.locator("article, li, div").filter(
        has_text=re.compile(r"\bACHETER\b", re.I)
    ).filter(
        has_text=re.compile(r"À\s*PARTIR\s*DE", re.I)
    )

    total = await containers.count()
    log(f"[SCRAPE] Blocs candidats (ACHETER + À PARTIR DE) : {total}")

    leading_foiler = 0
    checked = 0

    for i in range(min(total, MAX_SCAN_ITEMS)):
        item = containers.nth(i)
        data = await extract_title_and_price(item)
        checked += 1

        if not data:
            # prix non parsé → on ignore silencieusement
            continue

        title = data["title"] or ""
        if FOILER_RE.search(title):
            if leading_foiler == i:  # toujours en tête
                leading_foiler += 1
            log(f"[FILTER] Ligne {i}: titre = '{title}' → Foiler → ignorée.")
            continue

        # sécurité supplémentaire : si le bloc contient explicitement "Foiler" dans son texte
        try:
            if await item.locator("text=/foiler/i").count() > 0:
                if leading_foiler == i:
                    leading_foiler += 1
                log(f"[FILTER] Ligne {i}: sous-élément 'Foiler' détecté → ignorée.")
                continue
        except Exception:
            pass

        # On a trouvé la 1ʳᵉ carte non-Foiler
        log(f"[CHECK] Première non-Foiler trouvée à la ligne {i} : {data['price']:.2f} € — {data['title']} — {data['url']}")
        if leading_foiler:
            log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
        return data

    if leading_foiler:
        log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
    log(f"[INFO] Aucune carte non-Foiler trouvée après examen de {checked} blocs.")
    return None

# ------------------------- MAIN LOOP -------------------------
async def main():
    global best_seen_price, best_seen_title

    log("[BOOT] Surveillance Altered marketplace (1ʳᵉ carte non-Foiler)")

    if not os.path.exists(STATE_PATH):
        log(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        # USER_AGENT : fallback si invalide
        context_kwargs = dict(locale="fr-FR", storage_state=STATE_PATH)
        ua = (USER_AGENT or "").strip()
        if ua and all(32 <= ord(c) <= 126 for c in ua):
            context_kwargs["user_agent"] = ua
        else:
            if ua:
                log("[UA][WARN] USER_AGENT invalide. Ignoré (UA par défaut).")

        context = await browser.new_context(**context_kwargs)
        context.set_default_timeout(REQUEST_TIMEOUT_MS)
        page = await context.new_page()

        while True:
            try:
                log(f"[LOOP] {datetime.now().strftime('%H:%M:%S')} Chargement…")

                ok = await goto_with_retries(page, TARGET_URL)
                if not ok:
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                if page.url.startswith("https://auth.altered.gg"):
                    log("[AUTH][ERR] Session expirée / login requis. Regénère storage_state.json.")
                    await asyncio.sleep(max(POLL_SECONDS, 60))
                    continue

                try:
                    await page.evaluate("window.scrollTo(0, 600)")
                except Exception:
                    pass

                card = await find_first_non_foiler(page)

                if not card:
                    log("[INFO] Pas de carte valide détectée sur cette itération.")
                else:
                    price = card["price"]
                    title = card["title"]
                    url = card["url"]
                    log(f"[INFO] Min courant (1ʳᵉ non-Foiler): {price:.2f} € — {title} — {url}")

                    # Alerte uniquement si nouveau plus bas
                    if (best_seen_price is math.inf) or (price < best_seen_price - 1e-9):
                        log(f"[ALERT] Nouveau plus bas {price:.2f} € "
                            f"(ancien {best_seen_price if best_seen_price < math.inf else '∞'})")
                        send_ifttt(title or "Carte unique", price, url)
                        best_seen_price, best_seen_title = price, title

            except PWTimeout:
                log("[WARN] Timeout d'action Playwright (goto / selector).")
            except Exception as e:
                log(f"[ERR] Boucle : {e}\n{traceback.format_exc()}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())





