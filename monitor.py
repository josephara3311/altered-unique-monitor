# monitor.py
import os, re, math, asyncio, traceback, requests
from datetime import datetime
from typing import Optional, Dict, List, Tuple
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

# Nouveaux gardes-fous pour l’auto-scroll
MAX_SCROLL_STEPS   = int(os.getenv("MAX_SCROLL_STEPS", "30"))   # nb max de scrolls supplémentaires
SCROLL_PAUSE_MS    = int(os.getenv("SCROLL_PAUSE_MS", "800"))   # pause entre scrolls (ms)
NO_GROWTH_RETRIES  = int(os.getenv("NO_GROWTH_RETRIES", "3"))   # scrolls autorisés sans apparition de nouvelles tuiles
# -------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

FOILER_RE = re.compile(r"\b(foiler|qr\s*code|code\s*qr|foil)\b", re.I)
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

# ---------- Détection "Foiler" multi-signaux ----------
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
        # attributs et classes
        for sel in ["*", "img", "svg", "[role=img]", "[aria-label]", "[title]"]:
            loc = item.locator(sel)
            n = min(await loc.count(), 6)
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

# ---------- Récupère {title, price, url} pour une tuile ----------
async def extract_card_data(item) -> Optional[Dict]:
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

    # URL (si on trouve un lien "détail" plausible, sinon URL liste)
    url = TARGET_URL
    try:
        links = item.locator("a")
        nlinks = await links.count()
        for k in range(nlinks):
            href = await links.nth(k).get_attribute("href")
            if not href:
                continue
            low = href.lower()
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

# ---------- Parcours avec auto-scroll : trouver 2 cartes non-Foiler ----------
async def pick_two_non_foiler_with_scroll(page) -> List[Dict]:
    """
    Ne retient que les tuiles de premier niveau: elles doivent contenir
    - 'À PARTIR DE'
    - ET un bouton 'ACCÉDER AU DÉTAIL' (exclut les lignes 'offre' internes).
    On scrolle progressivement tant qu'on n'a pas trouvé 2 non-Foiler.
    """
    detail_btn = page.get_by_text(re.compile(r"ACCÉDER AU DÉTAIL", re.I))

    def build_locator():
        return page.locator("article, li, div").filter(
            has_text=re.compile(r"À\s*PARTIR\s*DE", re.I)
        ).filter(
            has=detail_btn
        )

    containers = build_locator()
    total = await containers.count()
    log(f"[SCRAPE] Tuiles candidates (badge + bouton DÉTAIL) : {total}")

    selected: List[Dict] = []
    i = 0
    last_total = total
    no_growth = 0
    leading_foiler = 0
    first_seen = True

    while len(selected) < 2:
        # Parcourir ce qui est déjà chargé
        while i < total and len(selected) < 2:
            item = containers.nth(i)

            # première tuile => log de sécu
            if first_seen:
                first_is_foiler = await is_foiler_block(item)
                if first_is_foiler:
                    log("[CHECK] Première tuile visible = Foiler/QR → ignorée.")
                    leading_foiler += 1
                else:
                    log("[CHECK] Première tuile visible = OK (non-Foiler).")
                first_seen = False

            # Skip Foiler
            if await is_foiler_block(item):
                if leading_foiler == i:
                    leading_foiler += 1
                log(f"[FILTER] Tuile {i}: Foiler/QR → ignorée.")
                i += 1
                continue

            # Extraire données
            data = await extract_card_data(item)
            if not data:
                # prix non parsé
                i += 1
                continue

            selected.append(data)
            log(f"[PICK] #{len(selected)} → {data['price']:.2f} € — {data['title']} — {data['url']}")
            i += 1

        if len(selected) >= 2:
            break

        # Auto-scroll pour charger plus de tuiles
        if no_growth >= NO_GROWTH_RETRIES or (i >= total and (i > 0) and (no_growth >= NO_GROWTH_RETRIES)):
            log("[SCROLL][STOP] Plus de croissance détectée, on s'arrête.")
            break

        # Scroll d'un écran vers le bas
        await page.evaluate("window.scrollBy(0, Math.max(window.innerHeight, 800))")
        await page.wait_for_timeout(SCROLL_PAUSE_MS)

        # Mise à jour du locator (DOM évolutif)
        containers = build_locator()
        total = await containers.count()

        if total > last_total:
            log(f"[SCROLL] Nouvelles tuiles chargées : {last_total} → {total}")
            last_total = total
            no_growth = 0
        else:
            no_growth += 1
            log(f"[SCROLL] Pas de nouvelles tuiles (essai {no_growth}/{NO_GROWTH_RETRIES}).")

        # garde-fou global
        MAX_SCROLL_STEPS_local = MAX_SCROLL_STEPS
        MAX_SCROLL_STEPS_local -= 1
        if MAX_SCROLL_STEPS_local <= 0:
            log("[SCROLL][STOP] MAX_SCROLL_STEPS atteint.")
            break

    if leading_foiler:
        log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
    log(f"[INFO] Cartes retenues (non-Foiler) : {len(selected)} / 2")
    return selected

def min_from_selected(cards: List[Dict]) -> Tuple[Optional[Dict], Optional[float]]:
    if not cards:
        return None, None
    best = min(cards, key=lambda c: c["price"])
    return best, best["price"]

# ------------------------- MAIN LOOP -------------------------
async def main():
    global best_seen_price, best_seen_title

    log("[BOOT] Surveillance Altered marketplace (2 premières cartes non-Foiler avec auto-scroll)")

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

                # petit scroll initial pour déclencher lazy-load
                try:
                    await page.evaluate("window.scrollTo(0, 400)")
                except Exception:
                    pass

                selected = await pick_two_non_foiler_with_scroll(page)

                if selected:
                    for i, c in enumerate(selected, 1):
                        log(f"[TOP{i}] {c['price']:.2f} € — {c['title']} — {c['url']}")
                else:
                    log("[TOP2] Aucune carte non-Foiler trouvée (même après scroll).")

                best_card, min_price = min_from_selected(selected)
                if not best_card:
                    log("[INFO] Aucune vraie carte trouvée.")
                else:
                    log(f"[INFO] Min courant (sur {len(selected)} carte(s)) : {min_price:.2f} € — "
                        f"{best_card['title']} — {best_card['url']}")
                    if (best_seen_price is math.inf) or (min_price < best_seen_price - 1e-9):
                        log(f"[ALERT] Nouveau plus bas {min_price:.2f} € "
                            f"(ancien {best_seen_price if best_seen_price < math.inf else '∞'})")
                        send_ifttt(best_card['title'] or "Carte unique", min_price, best_card['url'])
                        best_seen_price, best_seen_title = min_price, best_card['title']

            except PWTimeout:
                log("[WARN] Timeout d'action Playwright (goto / selector).")
            except Exception as e:
                log(f"[ERR] Boucle : {e}\n{traceback.format_exc()}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())






