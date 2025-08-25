# monitor.py
import os, re, math, json, asyncio, traceback, requests
from datetime import datetime
from typing import Optional, Dict
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --------- Config (via variables d'env Render) ----------
TARGET_URL   = os.getenv("TARGET_URL", "https://www.altered.gg/fr-fr/cards/market?order[price]=ASC&rarity[]=UNIQUE")
IFTTT_KEY    = os.getenv("IFTTT_KEY", "")
IFTTT_EVENT  = os.getenv("IFTTT_EVENT", "altered_min_price")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
USER_AGENT   = os.getenv("USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
STATE_PATH   = os.getenv("STORAGE_STATE_PATH", "/etc/secrets/storage_state.json")

REQUEST_TIMEOUT_MS    = int(os.getenv("REQUEST_TIMEOUT_MS", "45000"))
WAIT_BADGE_TIMEOUT_MS = int(os.getenv("WAIT_BADGE_TIMEOUT_MS", "25000"))
MAX_GOTO_RETRIES      = int(os.getenv("MAX_GOTO_RETRIES", "5"))

# Scroll/lazy-load
MAX_SCROLL_STEPS        = int(os.getenv("MAX_SCROLL_STEPS", "120"))
SCROLL_PAUSE_MS         = int(os.getenv("SCROLL_PAUSE_MS", "900"))
NO_GROWTH_RETRIES       = int(os.getenv("NO_GROWTH_RETRIES", "10"))
NO_HEIGHT_GROWTH_RETRY  = int(os.getenv("NO_HEIGHT_GROWTH_RETRY", "8"))

# Prix sous lequel on exige une vérification de fiche détail /cards/
VERIFY_BELOW_EUR        = float(os.getenv("VERIFY_BELOW_EUR", "1.50"))
DETAIL_TIMEOUT_MS       = int(os.getenv("DETAIL_TIMEOUT_MS", "10000"))
# -------------------------------------------------------

STATE_FILE = "/tmp/altered_state.json"
best_seen_price = math.inf
best_seen_title = None

# IMPORTANT: on ne matche plus "foil", seulement "foiler"
FOILER_RE_STRICT = re.compile(r"\bfoiler\b", re.I)
DISPO_RE  = re.compile(r"\bDisponible\s+à\b", re.I)  # lignes d'offres internes
PRICE_RE  = re.compile(r"À\s*PARTIR\s*DE\s*([0-9]+(?:[.,][0-9]{1,2})?)\s*€", re.I)

def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# ---------- persistance ----------
def _load_state():
    global best_seen_price, best_seen_title
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        best_seen_price = float(data.get("best_price", math.inf))
        best_seen_title = data.get("best_title")
        log(f"[STATE] Reprise meilleur prix: {best_seen_price if best_seen_price<math.inf else '∞'}")
    except Exception:
        log("[STATE] Aucun état précédent (nouveau déploiement).")

def _save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"best_price": best_seen_price, "best_title": best_seen_title, "ts": datetime.utcnow().isoformat()}, f)
    except Exception as e:
        log(f"[STATE][WARN] Impossible d'écrire l'état: {e}")

# ---------- IFTTT ----------
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

# ---------- parsing ----------
def parse_price(text: str) -> Optional[float]:
    t = text.replace("\xa0", " ").replace("\u202f", " ")
    m = PRICE_RE.search(t) or re.search(r"(\d+(?:[.,][0-9]{1,2})?)\s*€", t)
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
            # Il se peut que la page ne contienne pas encore le badge de prix :
            await page.wait_for_load_state("domcontentloaded")
            return True
        except PWTimeout as e:
            last_exc = e
            log(f"[NAV][WARN] Timeout goto/wait (try {i}) : {e}")
            await asyncio.sleep(1.2)
        except Exception as e:
            last_exc = e
            log(f"[NAV][WARN] Exception goto (try {i}) : {e}")
            await asyncio.sleep(1.2)
    log(f"[NAV][ERR] Échec navigation : {last_exc}")
    return False

# ---------- util: rendre absolu ----------
def abs_url(base: str, href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return urljoin(base, href)

# ---------- filtres ----------
async def is_foiler_block(item) -> bool:
    """
    Politique stricte: on considère Foiler uniquement si on voit le terme 'Foiler' en clair
    dans ce bloc (texte visible, badge, title/alt/aria-label), ou si un lien contient 'foiler'
    en mot complet. On NE match PAS 'foil' seul.
    """
    try:
        # Texte visible du bloc
        visible_txt = (await item.inner_text()).replace("\xa0"," ").replace("\u202f"," ")
        if FOILER_RE_STRICT.search(visible_txt):
            return True
    except Exception:
        pass

    # Petits attributs sur descendants
    try:
        loc = item.locator("*")
        n = min(await loc.count(), 12)
        for i in range(n):
            el = loc.nth(i)
            for attr in ["aria-label", "title", "alt"]:
                v = await el.get_attribute(attr)
                if v and FOILER_RE_STRICT.search(v):
                    return True
            cls = await el.get_attribute("class")
            if cls and FOILER_RE_STRICT.search(cls):
                return True
    except Exception:
        pass

    # Liens: on ne blackliste que si 'foiler' est un segment/param clair
    try:
        links = item.locator("a")
        n = await links.count()
        for i in range(min(n, 8)):
            href = await links.nth(i).get_attribute("href") or ""
            if FOILER_RE_STRICT.search(href):
                return True
    except Exception:
        pass

    return False

# ---------- résolution vers /cards/ ----------
async def resolve_to_card_detail(context, url: Optional[str]) -> Optional[str]:
    """
    Garantit qu'on renvoie une URL de fiche /cards/... si possible.
    - Si url est déjà /cards/, ok.
    - Sinon ouvre la page, tente <link rel="canonical">, puis le premier a[href*="/cards/"] visible/clickable.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if "/cards/" in parsed.path:
            return url  # déjà une fiche
    except Exception:
        pass

    page = await context.new_page()
    try:
        await page.goto(url, timeout=DETAIL_TIMEOUT_MS, wait_until="domcontentloaded")
        # 1) canonical
        try:
            canonical = await page.locator('link[rel="canonical"]').get_attribute("href")
        except Exception:
            canonical = None
        if canonical and "/cards/" in canonical:
            final_url = abs_url(url, canonical)
            await page.close()
            return final_url

        # 2) premier lien vers /cards/
        link = page.locator('a[href*="/cards/"]').first
        if await link.count() > 0:
            href = await link.get_attribute("href")
            final_url = abs_url(url, href)
            await page.close()
            return final_url

        await page.close()
        return url  # fallback: rien de mieux
    except Exception:
        try:
            await page.close()
        except Exception:
            pass
        return url

# ---------- vérification de page détail (anti-Foiler) ----------
async def verify_not_foiler_by_detail(context, url: Optional[str]) -> bool:
    """Retourne True si la fiche /cards/... ne contient pas 'Foiler' (visible). False sinon/échec."""
    if not url:
        return False
    detail = await resolve_to_card_detail(context, url)
    if not detail:
        return False
    try:
        page = await context.new_page()
        await page.goto(detail, timeout=DETAIL_TIMEOUT_MS, wait_until="domcontentloaded")

        # Si n'importe quel élément avec texte 'Foiler' est visible → Foiler
        try:
            foiler_visible = await page.locator("text=/\\bFoiler\\b/i").is_visible(timeout=2000)
        except Exception:
            foiler_visible = False

        body_text = ""
        try:
            body_text = (await page.inner_text("body")).lower()
        except Exception:
            pass

        await page.close()
        if foiler_visible or ("foiler" in body_text):
            return False
        return True
    except Exception:
        return False

# ---------- extraction ----------
async def extract_title_price_url(base_url: str, item) -> Optional[Dict]:
    try:
        txt = await item.inner_text()
    except PWTimeout:
        return None
    if DISPO_RE.search(txt):  # ignore offres internes “Disponible à …”
        return None
    price = parse_price(txt)
    if price is None:
        return None

    # Titre
    title = None
    try:
        t_el = item.locator("h1, h2, h3, .title, [data-testid=card-title]").first
        if await t_el.count() > 0:
            title = (await t_el.inner_text()).strip()
    except Exception:
        pass
    if not title:
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        title = next((l for l in lines
                      if "À PARTIR" not in l.upper()
                      and "ACHETER" not in l.upper()
                      and "VENDRE"  not in l.upper()
                      and not DISPO_RE.search(l)), "Carte unique")

    # URL fiche: on privilégie explicitement /cards/
    detail_url = None
    try:
        link_cards = item.locator('a[href*="/cards/"]').first
        if await link_cards.count() > 0:
            href = await link_cards.get_attribute("href")
            detail_url = abs_url(base_url, href)
        else:
            # fallback: premier lien "propre" non-foiler
            links = item.locator("a")
            n = await links.count()
            for k in range(n):
                href = await links.nth(k).get_attribute("href")
                if not href:
                    continue
                low = href.lower()
                if "foiler" in low or "disponible" in low:
                    continue
                detail_url = abs_url(base_url, href)
                break
    except Exception:
        pass

    return {"title": title, "price": price, "detail_url": detail_url}

# ---------- scroll + pick ----------
async def find_first_non_foiler_with_scroll(context, page) -> Optional[Dict]:
    def build_locator():
        # articles/tiles qui ont ACHETER + À PARTIR DE
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
    no_height_growth = 0
    steps = 0

    async def scroll_to_bottom_and_get_height():
        return await page.evaluate("""
            () => {
                const el = document.scrollingElement || document.documentElement;
                const before = el.scrollHeight;
                el.scrollTo(0, el.scrollHeight);
                return before;
            }
        """)

    prev_height = await scroll_to_bottom_and_get_height()
    await page.wait_for_timeout(SCROLL_PAUSE_MS)

    while True:
        containers = build_locator()
        total = await containers.count()

        while seen < total:
            item = containers.nth(seen)

            if first_seen:
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

            try:
                txt = await item.inner_text()
            except Exception:
                txt = ""
            if DISPO_RE.search(txt):
                seen += 1
                continue

            if await is_foiler_block(item):
                if leading_foiler == seen:
                    leading_foiler += 1
                log(f"[FILTER] Ligne {seen}: Foiler → ignorée.")
                seen += 1
                continue

            data = await extract_title_price_url(TARGET_URL, item)
            seen += 1
            if not data:
                continue

            # *** Vérification détail systématique si prix ≤ seuil ***
            if data["price"] <= VERIFY_BELOW_EUR:
                # Résoudre vers une vraie fiche /cards/ puis contrôler Foiler
                card_url = await resolve_to_card_detail(context, data["detail_url"])
                ok = await verify_not_foiler_by_detail(context, card_url)
                if not ok:
                    log(f"[VERIFY] {data['price']:.2f} € ≤ {VERIFY_BELOW_EUR:.2f} → fiche=Foiler/indispo → ignorée.")
                    continue
                # si ok, on remplace l'URL par la fiche résolue
                data["detail_url"] = card_url

            url = data["detail_url"] or TARGET_URL
            log(f"[PICK] 1ʳᵉ non-Foiler: {data['price']:.2f} € — {data['title']} — {url}")
            if leading_foiler:
                log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
            return {"title": data["title"], "price": data["price"], "url": url}

        if steps >= MAX_SCROLL_STEPS:
            log("[SCROLL][STOP] MAX_SCROLL_STEPS atteint.")
            break

        old_height = prev_height
        prev_height = await scroll_to_bottom_and_get_height()
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        steps += 1

        containers = build_locator()
        total = await containers.count()
        grew = total > last_total
        height_grew = (await page.evaluate(
            "document.scrollingElement ? document.scrollingElement.scrollHeight : document.documentElement.scrollHeight"
        )) > old_height

        if grew:
            log(f"[SCROLL] Nouvelles cartes chargées : {last_total} → {total}")
            last_total = total
            no_growth = 0
        else:
            no_growth += 1
            log(f"[SCROLL] Pas de nouvelles cartes (essai {no_growth}/{NO_GROWTH_RETRIES}).")

        if height_grew:
            no_height_growth = 0
        else:
            no_height_growth += 1
            log(f"[SCROLL] Hauteur inchangée (essai {no_height_growth}/{NO_HEIGHT_GROWTH_RETRY}).")

        if no_growth >= NO_GROWTH_RETRIES and no_height_growth >= NO_HEIGHT_GROWTH_RETRY:
            log("[SCROLL][STOP] Plus de croissance (items & hauteur), on s'arrête.")
            break

    if leading_foiler:
        log(f"[INFO] Foiler en tête de liste ignorés : {leading_foiler}")
    log("[INFO] Aucune carte non-Foiler trouvée (même après scroll).")
    return None

# ------------------------- MAIN LOOP -------------------------
async def main():
    global best_seen_price, best_seen_title

    log("[BOOT] Altered monitor v5 (Foiler strict, fiche /cards/ forcée, vérif ≤ seuil, scroll robuste, persistance)")
    _load_state()

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

                try:
                    await page.evaluate("window.scrollTo(0, 400)")
                except Exception:
                    pass

                card = await find_first_non_foiler_with_scroll(context, page)

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
                        _save_state()
                    else:
                        log(f"[INFO] Pas d'alerte: {price:.2f} € ≥ meilleur vu {best_seen_price:.2f} €")

            except PWTimeout:
                log("[WARN] Timeout Playwright.")
            except Exception as e:
                log(f"[ERR] Boucle : {e}\n{traceback.format_exc()}")

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
