import os
import time
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright

# === CONFIG ===
TARGET_URL = os.getenv(
    "TARGET_URL",
    "https://www.altered.gg/fr-fr/cards/market?order[price]=ASC&rarity[]=UNIQUE"
)

# Fichier de session (stocké en Secret File sur Render)
STATE_PATH = os.getenv("STORAGE_STATE_PATH", "storage_state.json")

# IFTTT
IFTTT_KEY = os.getenv("IFTTT_KEY")
IFTTT_EVENT = os.getenv("IFTTT_EVENT", "altered_min_price")

# Polling
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# User-Agent pour ressembler à un vrai navigateur
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

# === FUNCTIONS ===
def notify_ifttt(title, price, link):
    if not IFTTT_KEY:
        print("[WARN] Pas de clé IFTTT configurée")
        return
    url = f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/with/key/{IFTTT_KEY}"
    payload = {"value1": title, "value2": price, "value3": link}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print("[IFTTT]", r.status_code, r.text)
    except Exception as e:
        print("[ERROR] IFTTT:", e)


def run_loop():
    last_seen = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="fr-FR",
            storage_state=STATE_PATH
        )
        page = context.new_page()

        while True:
            try:
                print("\n[LOOP]", datetime.now().strftime("%H:%M:%S"), "Chargement…")
                page.goto(TARGET_URL, timeout=60000)
                page.wait_for_load_state("networkidle")

                # Sélectionne toutes les cartes affichées
                cards = page.locator("a[href*='/cards/']").all_text_contents()
                print("[DEBUG] Nb cartes détectées:", len(cards))

                cheapest_card = None
                cheapest_price = 9999

                # On parcourt les cartes
                for card in page.locator("a[href*='/cards/']").all():
                    text = card.inner_text().strip()
                    if not text:
                        continue
                    if "Foiler" in text:
                        continue  # exclure les foilers

                    price_spans = card.locator("span:has-text('€')").all_text_contents()
                    if not price_spans:
                        continue

                    try:
                        price_str = (
                            price_spans[0]
                            .replace("€", "")
                            .replace(",", ".")
                            .strip()
                        )
                        price = float(price_str)
                    except:
                        continue

                    if price < cheapest_price:
                        cheapest_price = price
                        cheapest_card = (text, price, card.get_attribute("href"))

                if cheapest_card:
                    title, price, link = cheapest_card
                    full_link = "https://www.altered.gg" + link
                    print(f"[INFO] Min courant: {title} - {price}€")
                    if last_seen is None or price < last_seen:
                        print("[ALERT] Nouvelle carte moins chère détectée !")
                        notify_ifttt(title, price, full_link)
                        last_seen = price
                else:
                    print("[WARN] Aucune carte détectée (ou que des Foilers)")

            except Exception as e:
                print("[ERROR LOOP]", e)

            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    print("[START] Surveillance Altered marketplace")
    run_loop()



