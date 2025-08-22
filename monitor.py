# monitor.py
import os, re, time, math, traceback, requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ---------- Config depuis les variables d'environnement ----------
TARGET_URL   = os.getenv("TARGET_URL")  # ex: https://www.altered.gg/fr-fr/cards/market?order[price]=ASC&rarity[]=UNIQUE
IFTTT_KEY    = os.getenv("IFTTT_KEY")
IFTTT_EVENT  = os.getenv("IFTTT_EVENT", "altered_min_price")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
USER_AGENT   = os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")
STATE_PATH   = os.getenv("STORAGE_STATE_PATH", "storage_state.json")  # sur Render: /etc/secrets/storage_state.json
# -----------------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

def send_ifttt_push(title: str, price: float, url: str):
    """Envoie une notification push via IFTTT Webhooks."""
    if not IFTTT_KEY:
        print("[WARN] IFTTT_KEY manquant : notif non envoyée.", flush=True)
        return
    try:
        r = requests.post(
            f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/json/with/key/{IFTTT_KEY}",
            json={"value1": f"Nouvelle carte moins chère : {title}",
                  "value2": f"{price:.2f} €",
                  "value3": url},
            timeout=15,
        )
        r.raise_for_status()
        print("[OK] Notification IFTTT envoyée.", flush=True)
    except Exception as e:
        print(f"[ERR] IFTTT: {e}", flush=True)

def parse_price(text: str):
    """Extrait un prix depuis un texte (gère espaces insécables et , / .)."""
    t = text.replace("\xa0", " ").replace("\u202f", " ")
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*€", t)
    return float(m.group(1).replace(",", ".")) if m else None

def find_min_price_card(page):
    """
    Scanne la liste :
      - part des blocs qui contiennent un bouton 'ACHETER'
      - ignore les 'Foiler'
      - lit le badge bleu 'À PARTIR DE X €'
    Retourne (titre_min, prix_min) ou (None, None).
    """
    page.wait_for_load_state("networkidle", timeout=20000)

    # Conteneurs de lignes-cartes : heuristique = blocs qui ont 'ACHETER'
    rows = page.locator("div").filter(has_text=re.compile(r"\bACHETER\b", re.I))
    total = rows.count()
    print(f"[DEBUG] Lignes détectées (ACHETER): {total}", flush=True)

    # Montrer 1-2 extraits pour debugger
    for i in range(min(total, 2)):
        snippet = rows.nth(i).inner_text()[:300].replace("\n", " | ")
        print(f"[DEBUG] Ligne {i} (extrait): {snippet}", flush=True)

    min_price, min_title = math.inf, None

    for i in range(total):
        row = rows.nth(i)

        # 1) Exclure explicitement les Foiler
        if row.get_by_text(re.compile(r"\bFoiler\b", re.I)).count() > 0:
            # print(f"[DEBUG] Ligne {i} ignorée (Foiler)", flush=True)
            continue

        # 2) Repérer le badge bleu "À PARTIR DE 0,24 €"
        price_nodes = row.get_by_text(re.compile(r"À\s*PARTIR\s*DE\s*[0-9.,]+\s*€", re.I))
        if price_nodes.count() == 0:
            # Pas de badge, on log un mini extrait pour comprendre
            mini = row.inner_text()[:140].replace("\n", " | ")
            print(f"[DEBUG] Pas de badge bleu sur la ligne {i}: {mini}", flush=True)
            continue

        price_text = price_nodes.first.inner_text().strip()
        price = parse_price(price_text)
        if price is None:
            print(f"[DEBUG] Badge trouvé mais prix non parsé: '{price_text}'", flush=True)
            continue

        # 3) Deviner un titre potable (ligne informative hors badges/boutons/foiler)
        block_text = row.inner_text()
        lines = [l.strip() for l in block_text.splitlines() if l.strip()]
        title_guess = next(
            (l for l in lines
             if "À PARTIR" not in l.upper()
             and "ACHETER" not in l.upper()
             and "VENDRE"  not in l.upper()
             and not re.search(r"\bFoiler\b", l, re.I)),
            "Carte unique"
        )

        # 4) Garder le minimum
        if price < min_price:
            min_price, min_title = price, title_guess

    return (None, None) if min_price == math.inf else (min_title, min_price)

def main():
    global best_seen_price, best_seen_title

    print("[START] worker démarré", flush=True)

    if not os.path.exists(STATE_PATH):
        print(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}", flush=True)
        print("           -> Sur Render : Settings > Environment > Secret Files (storage_state.json)", flush=True)
        return

    with sync_playwright() as p:
        print("[PLAYWRIGHT] Lancement de Chromium…", flush=True)
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR", storage_state=STATE_PATH)
        page = context.new_page()

        while True:
            try:
                print("[LOOP] Chargement de la page…", flush=True)
                page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")

                # Si la session a expiré -> redirection auth
                if page.url.startswith("https://auth.altered.gg"):
                    print("[AUTH][ERR] Session expirée / 2FA requis. Regénère storage_state.json et remets-le sur Render.", flush=True)
                    time.sleep(max(POLL_SECONDS, 60))
                    continue

                # Anti lazy-load : faire apparaître les 1res cartes
                page.wait_for_load_state("networkidle", timeout=20000)
                try:
                    page.evaluate("window.scrollTo(0, 400)")
                except Exception:
                    pass

                title, price = find_min_price_card(page)

                if price is None:
                    print("[INFO] Aucun prix détecté.", flush=True)
                else:
                    print(f"[INFO] Min courant: {price:.2f} € — {title}", flush=True)
                    if price < best_seen_price - 1e-6:
                        print(f"[ALERT] Nouveau plus bas {price:.2f} € "
                              f"(ancien {best_seen_price if best_seen_price < math.inf else '∞'})", flush=True)
                        send_ifttt_push(title or "Carte unique", price, TARGET_URL)
                        best_seen_price, best_seen_title = price, title

            except PWTimeout:
                print("[WARN] Timeout de chargement.", flush=True)
            except Exception as e:
                print(f"[ERR] Exception boucle: {e}\n{traceback.format_exc()}", flush=True)

            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()


