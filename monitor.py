# monitor.py — async / Render-ready
import os, re, time, math, asyncio, traceback, requests
from datetime import datetime
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
# Secret File monté par Render ici :
STATE_PATH   = os.getenv("STORAGE_STATE_PATH", "/etc/secrets/storage_state.json")
# -------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

def send_ifttt(title: str, price: float, link: str):
    if not IFTTT_KEY:
        print("[WARN] IFTTT_KEY manquant — notif non envoyée.", flush=True)
        return
    try:
        r = requests.post(
            f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/json/with/key/{IFTTT_KEY}",
            json={"value1": title, "value2": f"{price:.2f} €", "value3": link},
            timeout=15,
        )
        print("[IFTTT]", r.status_code, r.text[:200])
    except Exception as e:
        print("[ERR] IFTTT:", e, flush=True)

def parse_price(text: str):
    t = text.replace("\xa0", " ").replace("\u202f", " ")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*€", t)
    return float(m.group(1).replace(",", ".")) if m else None

async def find_min_price_card(page):
    """
    Heuristique :
      - on part des blocs qui ont un bouton 'ACHETER'
      - on ignore ceux contenant 'Foiler'
      - on lit le badge bleu 'À PARTIR DE X €'
    """
    await page.wait_for_load_state("networkidle")
    rows = page.locator("div").filter(has_text=re.compile(r"\bACHETER\b", re.I))
    total = await rows.count()
    print(f"[DEBUG] Lignes détectées (ACHETER): {total}", flush=True)

    # un petit extrait pour debug
    for i in range(min(2, total)):
        snippet = (await rows.nth(i).inner_text())[:300].replace("\n", " | ")
        print(f"[DEBUG] Ligne {i} (extrait): {snippet}", flush=True)

    min_price, min_title = math.inf, None

    for i in range(total):
        row = rows.nth(i)

        # exclure Foiler
        if await row.get_by_text(re.compile(r"\bFoiler\b", re.I)).count() > 0:
            continue

        # Badge bleu
        price_nodes = row.get_by_text(re.compile(r"À\s*PARTIR\s*DE\s*[0-9.,]+\s*€", re.I))
        if await price_nodes.count() == 0:
            mini = (await row.inner_text())[:140].replace("\n", " | ")
            print(f"[DEBUG] Pas de badge bleu sur la ligne {i}: {mini}", flush=True)
            continue

        price_text = (await price_nodes.first.inner_text()).strip()
        price = parse_price(price_text)
        if price is None:
            print(f"[DEBUG] Badge trouvé mais prix non parsé: '{price_text}'", flush=True)
            continue

        # deviner un titre lisible
        block_text = await row.inner_text()
        lines = [l.strip() for l in block_text.splitlines() if l.strip()]
        title_guess = next(
            (l for l in lines
             if "À PARTIR" not in l.upper()
             and "ACHETER" not in l.upper()
             and "VENDRE"  not in l.upper()
             and not re.search(r"\bFoiler\b", l, re.I)),
            "Carte unique"
        )

        if price < min_price:
            min_price, min_title = price, title_guess

    return (None, None) if min_price == math.inf else (min_title, min_price)

async def main():
    global best_seen_price, best_seen_title

    print("[START] Surveillance Altered marketplace", flush=True)

    if not os.path.exists(STATE_PATH):
        print(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}", flush=True)
        print("           -> Render > Environment > Secret Files : storage_state.json", flush=True)
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            storage_state=STATE_PATH
        )
        page = await context.new_page()

        while True:
            try:
                print(f"\n[LOOP] {datetime.now().strftime('%H:%M:%S')} Chargement…", flush=True)
                await page.goto(TARGET_URL, timeout=60000, wait_until="networkidle")

                # si la session a expiré
                if page.url.startswith("https://auth.altered.gg"):
                    print("[AUTH][ERR] Session expirée / login requis. Regénère storage_state.json.", flush=True)
                    await asyncio.sleep(max(POLL_SECONDS, 60))
                    continue

                # petit scroll pour déclencher le lazy-load éventuel
                try:
                    await page.evaluate("window.scrollTo(0, 400)")
                except Exception:
                    pass

                title, price = await find_min_price_card(page)

                if price is None:
                    print("[INFO] Aucun prix détecté.", flush=True)
                else:
                    print(f"[INFO] Min courant: {price:.2f} € — {title}", flush=True)
                    if price < best_seen_price - 1e-6:
                        print(f"[ALERT] Nouveau plus bas {price:.2f} € "
                              f"(ancien {best_seen_price if best_seen_price < math.inf else '∞'})", flush=True)
                        send_ifttt(title or "Carte unique", price, TARGET_URL)
                        best_seen_price, best_seen_title = price, title

            except PWTimeout:
                print("[WARN] Timeout chargement.", flush=True)
            except Exception as e:
                print(f"[ERR] Boucle: {e}\n{traceback.format_exc()}", flush=True)

            await asyncio.sleep(POLL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())




