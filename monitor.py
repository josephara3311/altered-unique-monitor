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
MAX_GOTO_RETRIES = int(os.getenv("MAX_GOTO_RETRIES", "5"))

# Scroll/lazy-load
MAX_SCROLL_STEPS   = int(os.getenv("MAX_SCROLL_STEPS", "40"))
SCROLL_PAUSE_MS    = int(os.getenv("SCROLL_PAUSE_MS", "600"))
NO_GROWTH_RETRIES  = int(os.getenv("NO_GROWTH_RETRIES", "4"))
# -------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

FOILER_RE = re.compile(r"\b(foiler|foil)\b", re.I)
PRICE_RE  = re.compile(r"À\s*PARTIR\s*DE\s*([0-9]+(?:[.,][0-9]{1,2})?)\s*€", re.I)
DISPO_RE  = re.compile(r"\bDisponible\s+à\b", re.I)  # lignes d'offres internes

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
    m = PRICE_RE.search(t) or re.search(r"(\d+(?:[.,]\d{1,2})?)\s*€", t)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
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
            await asyncio.sleep(1.5)
        except Exception as e:
            last_exc = e
            log(f"[NAV][WARN] Exception goto (try {i}) : {e}")
            await asyncio.sleep(1.5)
    log(f"[NAV][ERR] Échec navigation : {last_exc}")
    return False

# ---------- Détection Foiler robuste ----------
async def is_foiler_block(item) -> bool:
    try:
        txt = (await item.inner_text()).replace("\xa0"," ").replace("\u202f"," ")
    except Exception:
        txt = ""
    if FOILER_RE.search(txt):
        return True
    try:
        if await item.locator("text=/foiler/i").count() > 0:
            return True
    except Exception:
        pass
    try:
        # attributs alt/title/aria-label + classes
        loc = item.locator("*")
        n = min(await loc.count(), 8)
        for i in range(n):
            el = loc.nth(i)
            for attr in ["aria-label", "title", "alt"]:
                v = await el.get_attribute(attr)
                if v and FOILER_RE.search(v):
                    return True
            cls = await el.get_attribute("class")
            if cls and "foiler" in cls.lower():
                return True
    except Exception:
        pass
    try:
        # hrefs
        links = item.locator("a")
        n = await links.count()
        for i in range(min(n, 8)):
            href = await links.nth(i).get_attribute("href") or ""
            if FOILER_RE.search(href):
                return True
    except Exception:
        pass
    return False

# ---------- Extrait {title, price, url}; None si prix manquant ----------
async def extract_title_price_url(item) -> Optional[Dict]:
    try:
        txt = await item.inner_text()
    except PWTimeout:
        return None

    # ignorer explicitement les lignes d'offres internes
    if DISPO_RE.search(txt):
        return None

    price = parse_price(txt)
    if price is None:
        return None

    # titre
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
             and "VENDRE"  not in l.upper()
             and not DISPO_RE.search(l)),
            "Carte unique"
        )

    # URL (prend un lien le plus “propre” possible si dispo)
    url = TARGET_URL
    try:
        links = item.locator("a")
        n = await links.count()
        for k in range(n):
            href = await links.nth(k).get_attribute("href")
            if not href:
                continue
            low = href.lower()
            # on évite market/foiler/offre
            if ("market" in low and "cards" in low) or ("foiler" in low) or ("disponible" in low):
                continue
            if href.startswith("http"):
                url = href
            else:
                from urllib.parse import urljoin
                url = urljoin(TARGET_URL, href)
            break
    except Exception:
        pass

    return {"title": title, "price": price, "url": url}

# ---------- Trouve la 1re carte non-Foiler avec auto-scroll ----------
async def find_first_non_foiler_with_scroll(page) -> Optional[Dict]:
    """
    Candidats = blocs qui contiennent ACHETER + À PARTIR DE.
    On ignore :
      - tout bloc contenant 'Disponible à' (offres internes),
      - tout bloc détecté 'Foiler'.
    On scrolle tant qu'on n'a pas de résultat (avec gardes-fous).
    """
    def build_locator():
        return page.locator("article, li, div").filter(
            has_text=re.compile(r"\bACHETER\b", re.I)
        ).filter(
            has_text=re.compile(r"À\s*PARTIR\s*DE", re.I)
        )

    containers = build_locator()
    total = await containers.count()
    log(f"[SCRAPE] Blocs candidats (ACHETER + À PARTIR DE) : {total}")

    seen = 0
    leading_foiler = 0
    first_seen = True
    last_total = total
    no_growth = 0
    scrolls = 0

    while True:
        while seen < total:
            item = containers.nth(seen)

            # log de sécu sur le tout premier bloc
            if first_seen:
                # s'il est une offre interne, on ne compte pas dans leading_foiler
                try:
                    raw = await item.inner_text()
                except Exception:
                    raw = ""
                if DISPO_RE.search(raw):
                    log("[CHECK] Première ligne = 'Disponible à …' → ignorée (offre interne).")
                else:
                    if await is_foiler_block(item):
                        log("[CHECK] Première ligne = Foiler → ignorée.")
                        leading_foiler += 1
                    else:
                        log("[CHECK] Première ligne = OK (non-Foiler).")
                first_seen = False

            # ignorer offres internes
            try:
                txt = await item.inner_text()
            except Exception:
                txt = ""
            if DISPO_RE.search(txt):
                seen += 1
                continue

            # ignorer Foiler
            if await is_foiler_block(item):
                if leading_foiler == seen:
                    leading_foiler += 1
                log(f"[FILTER] Ligne {seen}: Foiler → ignorée.")
                seen += 1
                continue

            data = await extract_title_price_url(item)
            seen += 1
            if not data:
                continue

            log(f"[PICK] 1ʳᵉ non-Foiler: {data['price']:.2f} € — {data['title']} — {data['url']}")
            if leading_foiler:
                log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
            return data

        # pas trouvé → tenter de charger plus
        if scrolls >= MAX_SCROLL_STEPS:
            log("[SCROLL][STOP] MAX_SCROLL_STEPS atteint.")
            break

        await page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 900))")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        scrolls += 1

        containers = build_locator()
        total = await containers.count()
        if total > last_total:
            log(f"[SCROLL] Nouvelles cartes chargées : {last_total} → {total}")
            last_total = total
            no_growth = 0
        else:
            no_growth += 1
            log(f"[SCROLL] Pas de nouvelles cartes (essai {no_growth}/{NO_GROWTH_RETRIES}).")
            if no_growth >= NO_GROWTH_RETRIES:
                log("[SCROLL][STOP] Plus de croissance, on s'arrête.")
                break

    if leading_foiler:
        log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
    log("[INFO] Aucune carte non-Foiler trouvée (même après scroll).")
    return None

# ------------------------- MAIN LOOP -------------------------
async def main():
    global best_seen_price, best_seen_title

    log("[BOOT] Surveillance Altered marketplace (1ʳᵉ carte non-Foiler, auto-scroll, anti-'Disponible à')")

    if not os.path.exists(STATE_PATH):
        log(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
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

                # petit scroll initial
                try:
                    await page.evaluate("window.scrollTo(0, 400)")
                except Exception:
                    pass

                card = await find_first_non_foiler_with_scroll(page)

                if not card:
                    log("[INFO] Pas de carte utilisable sur cette itération.")
                else:
                    price, title, url = card["price"], card["title"], card["url"]
                    log(f"[INFO] Min courant (1ʳᵉ non-Foiler): {price:.2f} € — {title} — {url}")
                    if (best_seen_price is math.inf) or (price < best_seen_price - 1e-9):
                        log(f"[ALERT] Nouveau plus bas {price:.2f} € "
                            f"(ancien {best_seen_price if best_seen_price < math.inf else '∞'})")
                        send_ifttt(title or "Carte unique", price, url)
                        best_seen_price, best_seen_title = price, title

            except PWTimeout:
                log("[WARN] Timeout Playwright.")
            except Exception as e:
                log(f"[ERR] Boucle : {e}\n{traceback.format_exc()}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())





