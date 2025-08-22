# monitor.py
import os, re, time, math, traceback, requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# --- Config via variables d'environnement (Render > Settings > Environment) ---
TARGET_URL   = os.getenv("TARGET_URL")  # ex: https://www.altered.gg/fr-fr/cards/market?order[price]=ASC&rarity[]=UNIQUE
IFTTT_KEY    = os.getenv("IFTTT_KEY")   # ta clé Webhooks IFTTT
IFTTT_EVENT  = os.getenv("IFTTT_EVENT", "altered_min_price")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
USER_AGENT   = os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")

# Chemin du fichier de session (Render > Secret Files -> storage_state.json)
STATE_PATH   = os.getenv("STORAGE_STATE_PATH", "storage_state.json")

# ------------------------------------------------------------------------------

best_seen_price = math.inf
best_seen_title = None

def send_ifttt_push(title: str, price: float, url: str):
    """Envoie une notification push via IFTTT Webhooks."""
    if not IFTTT_KEY:
        print("[WARN] IFTTT_KEY manquant : aucune notif envoyée.", flush=True)
        return
    try:
        r = requests.post(
            f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/json/with/key/{IFTTT_KEY}",
            json={
                "value1": f"Nouvelle carte moins chère : {title}",
                "value2": f"{price:.2f} €",
                "value3": url
            },
            timeout=15
        )
        r.raise_for_status()
        print("[OK] Notification IFTTT envoyée.", flush=True)
    except Exception as e:
        print(f"[ERR] IFTTT: {e}", flush=True)

def parse_price(text: str):
    """Extrait un prix '12,34 €' ou '12.34 €' du texte."""
    m = re.search(r"(\d+[.,]?\d*)\s*€", text.replace("\xa0", " "))
    return float(m.group(1).replace(",", ".")) if m else None

def find_min_price_card(page):
    """
    Scanne la page courante, ignore les 'Foiler',
    renvoie (titre_min, prix_min) ou (None, None) si rien.
    """
    page.wait_for_load_state("networkidle", timeout=20000)
    # Heuristique: les cartes en vente ont le bouton "ACHETER"
    cards = page.locator("div").filter(has_text=re.compile(r"ACHETER", re.I))
    count = cards.count()
    min_price, min_title = math.inf, None

    for i in range(count):
        card = cards.nth(i)
        full_text = card.inner_text().strip()

        # Ignore les QR "Foiler"
        if re.search(r"\bFoiler\b", full_text, re.I):
            continue

        # Prix
        price = parse_price(full_text)
        if price is None:
            continue

        # Titre (meilleure estimation à partir des lignes de la carte)
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        title_guess = next(
            (l for l in lines
             if "À PARTIR" not in l.upper()
             and "ACHETER" not in l.upper()
             and not re.search(r"\bFoiler\b", l, re.I)),
            "Carte unique"
        )

        if price < min_price:
            min_price, min_title = price, title_guess

    return (None, None) if min_price == math.inf else (min_title, min_price)

def main():
    global best_seen_price, best_seen_title

    print("[START] worker démarré", flush=True)

    # Vérifie la présence du fichier de session
    if not os.path.exists(STATE_PATH):
        print(f"[AUTH][ERR] Fichier de session introuvable : {STATE_PATH}", flush=True)
        print("       -> Sur Render, ajoute-le via Settings > Secret Files (storage_state.json).", flush=True)
        return

    with sync_playwright() as p:
        print("[PLAYWRIGHT] Lancement de Chromium…", flush=True)
        # Important en environnement Docker/Render
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            storage_state=STATE_PATH
        )
        page = context.new_page()

        while True:
            try:
                print("[LOOP] Chargement de la page…", flush=True)
                page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")

                # Si la session a expiré, Altered peut rediriger vers auth.altered.gg
                if page.url.startswith("https://auth.altered.gg"):
                    print("[AUTH][ERR] Session expirée / login requis. "
                          "Régénère storage_state.json en local et remets-le sur Render.", flush=True)
                    time.sleep(max(POLL_SECONDS, 60))
                    continue

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
                print("[WARN] Timeout de chargement de page.", flush=True)
            except Exception as e:
                print(f"[ERR] Exception boucle: {e}\n{traceback.format_exc()}", flush=True)

            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()


