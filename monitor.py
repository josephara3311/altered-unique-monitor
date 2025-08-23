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
FOILER_RE = re.compile(r"\b(foiler|qr|qr\s*code|code\s*qr|foil)\b", re.I)

def parse_price_from_text(text: str) -> Optional[float]:
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

# ---------- DEBUG HELPERS ----------
async def _debug_dump_hrefs_and_html(item, idx):
    try:
        links = item.locator("a")
        n = await links.count()
        hrefs = []
        for k in range(min(n, 8)):
            h = await links.nth(k).get_attribute("href")
            if h:
                hrefs.append(h)
        html = await item.evaluate("el => el.outerHTML")
        html_snip = html.replace("\n", " ")[:400] if html else "(no html)"
        log(f"[DEBUG][L{idx}] hrefs={hrefs if hrefs else '[]'}")
        log(f"[DEBUG][L{idx}] html={html_snip}...")
    except Exception as e:
        log(f"[DEBUG][L{idx}] dump error: {e}")

async def is_foiler_card(item) -> bool:
    """
    Détecte Foiler/QR par plusieurs voies : texte, sous-éléments, attributs, classes, href.
    """
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
        for sel in ["*", "img", "svg", "[role=img]", "[aria-label]", "[title]"]:
            loc = item.locator(sel)
            n = min(await loc.count(), 6)
            for i in range(n):
                el = loc.nth(i)
                for attr in ["aria-label", "title", "alt"]:
                    val = await el.get_attribute(attr)
                    if val and FOILER_RE.search(val):
                        return True
                cls = await el.get_attribute("class")
                if cls and "foiler" in cls.lower():
                    return True
    except Exception:
        pass
    try:
        links = item.locator("a")
        n = await links.count()
        for i in range(min(n, 8)):
            href = await links.nth(i).get_attribute("href") or ""
            if FOILER_RE.search(href):
                return True
    except Exception:
        pass
    return False

# ---------- PICK: 2 premières vraies cartes (sans exiger /cards/) ----------
async def pick_first_two_cards(page):
    """
    Parcourt dans l'ordre les blocs (ACHETER + À PARTIR DE), ignore tous les Foiler/QR,
    et retient UNIQUEMENT les 2 premières cartes non-Foiler.
    Si un lien de détail plausible est trouvé, on l'utilise ; sinon on garde TARGET_URL.
    """
    containers = page.locator("article, li, div").filter(
        has_text=re.compile(r"\bACHETER\b", re.I)
    ).filter(
        has_text=re.compile(r"À\s*PARTIR\s*DE", re.I)
    )

    total = await containers.count()
    log(f"[SCRAPE] Conteneurs candidats (ACHETER + À PARTIR DE) : {total}")

    selected: List[Dict] = []
    foiler_ignored = 0
    first_seen = True
    first_non_foiler_seen = False

    for i in range(total):
        if len(selected) >= 2:
            break

        item = containers.nth(i)

        # Dump diag sur les 5 premières lignes
        if i < 5:
            await _debug_dump_hrefs_and_html(item, i)

        # Détection Foiler/QR robuste
        is_foiler = await is_foiler_card(item)

        # Log de sécurité sur la première ligne
        if first_seen:
            if is_foiler:
                log("[CHECK] Première carte visible = Foiler/QR → ignorée.")
            else:
                log("[CHECK] Première carte visible = OK (non-Foiler).")
                first_non_foiler_seen = True
            first_seen = False

        if is_foiler:
            foiler_ignored += 1
            if not first_non_foiler_seen:
                # ça veut dire qu'on a des foiler en tête de liste
                pass
            log(f"[FILTER] Ligne {i}: Foiler/QR → ignorée.")
            continue

        first_non_foiler_seen = True

        # Prix (badge)
        try:
            txt = await item.inner_text()
        except PWTimeout:
            continue
        price = parse_price_from_text(txt)
        if price is None:
            snippet = " | ".join([l.strip() for l in txt.splitlines() if l.strip()])[:180]
            log(f"[DEBUG] Ligne {i}: prix non parsé → ignorée. Extrait: {snippet}")
            continue

        # Titre
        title = None
        try:
            title_el = item.locator("h3, h2, .title, [data-testid=card-title]").first
            if await title_el.count() > 0:
                title = (await title_el.inner_text()).strip()
        except Exception:
            pass
        if not title:
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            title = next(
                (l for l in lines
                 if "À PARTIR" not in l.upper()
                 and "ACHETER" not in l.upper()
                 and "VENDRE" not in l.upper()
                 and not FOILER_RE.search(l)),
                "Carte unique"
            )

        # Essayer de récupérer une URL "détail" plausible ; sinon fallback = TARGET_URL
        url = TARGET_URL
        try:
            links = item.locator("a")
            nlinks = await links.count()
            for k in range(nlinks):
                href = await links.nth(k).get_attribute("href")
                if not href:
                    continue
                low = href.lower()
                # heuristique tolérante : un lien qui sort du market/foiler
                if ("cards" in low or "/card/" in low or "/items/" in low or "/item/" in low) and ("market" not in low) and ("foiler" not in low):
                    if href.startswith("http"):
                        url = href
                    else:
                        from urllib.parse import urljoin
                        url = urljoin(TARGET_URL, href)
                    break
        except Exception:
            pass

        selected.append({"title": title, "price": price, "url": url})
        log(f"[PICK] #{len(selected)} → {price:.2f} € — {title} — {url}")

    if foiler_ignored:
        log(f"[INFO] Foiler ignorés au total : {foiler_ignored}.")
    else:
        log("[INFO] Aucun Foiler ignoré sur cette page.")
    log(f"[INFO] Cartes retenues (non-Foiler) : {len(selected)} (max 2)")
    return selected

def min_from_selected(cards: List[Dict]) -> Tuple[Optional[Dict], Optional[float]]:
    if not cards:
        return None, None
    best = min(cards, key=lambda c: c["price"])
    return best, best["price"]

# ------------------------- MAIN LOOP -------------------------
async def main():
    global best_seen_price, best_seen_title

    log("[BOOT] Surveillance Altered marketplace (2 premières cartes non-Foiler)")

    if not os.path.exists(STATE_PATH):
        log(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context_kwargs = dict(locale="fr-FR", storage_state=STATE_PATH)
        UA = (USER_AGENT or "").strip()
        if UA and all(32 <= ord(c) <= 126 for c in UA):
            context_kwargs["user_agent"] = UA
        else:
            if UA:
                log("[UA][WARN] USER_AGENT invalide (caractères non-ASCII / retours ligne). Ignoré.")

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

                selected = await pick_first_two_cards(page)
                if selected:
                    for i, c in enumerate(selected[:2], 1):
                        log(f"[TOP2] #{i} {c['price']:.2f} € — {c['title']} — {c['url']}")
                else:
                    log("[TOP2] Aucune carte non-Foiler parmi le début de page.")

                best_card, min_price = min_from_selected(selected)
                if not best_card:
                    log("[INFO] Aucune vraie carte trouvée.")
                else:
                    log(f"[INFO] Min courant (sur 1–2 cartes) : {min_price:.2f} € — "
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






