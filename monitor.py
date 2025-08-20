import os, re, time, json, math
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# >>> URL exacte fournie <<<
TARGET_URL   = os.getenv("TARGET_URL", "https://www.altered.gg/fr-fr/cards/market?order[price]=ASC&rarity[]=UNIQUE")
IFTTT_KEY    = os.getenv("IFTTT_KEY")                       # ifttt.com/maker_webhooks > Documentation
IFTTT_EVENT  = os.getenv("IFTTT_EVENT", "altered_min_price")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))         # passe à 30 si l’hébergeur le permet
USER_AGENT   = os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")

best_seen_price = math.inf
best_seen_title = None

def send_ifttt_push(title:str, price:float, url:str):
    if not IFTTT_KEY:
        print("[WARN] IFTTT_KEY manquant, notification non envoyée.")
        return
    webhook_url = f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/json/with/key/{IFTTT_KEY}"
    payload = {"value1": f"Nouvelle carte moins chère : {title}",
               "value2": f"{price:.2f} €",
               "value3": url}
    try:
        r = requests.post(webhook_url, json=payload, timeout=15)
        r.raise_for_status()
        print("[OK] Notification IFTTT envoyée.")
    except Exception as e:
        print(f"[ERR] IFTTT: {e}")

def parse_price(text:str):
    m = re.search(r"(\d+[.,]?\d*)\s*€", text.replace("\xa0"," "))
    return float(m.group(1).replace(",", ".")) if m else None

def find_min_price_card(page):
    page.wait_for_load_state("networkidle", timeout=15000)
    cards = page.locator("div").filter(has_text=re.compile(r"ACHETER", re.I))
    min_price, min_title = math.inf, None
    for i in range(cards.count()):
        card = cards.nth(i)
        full_text = card.inner_text().strip()
        if re.search(r"\bFoiler\b", full_text, re.I):   # ignore les “Foiler”
            continue
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        title_guess = next((l for l in lines if "À PARTIR" not in l.upper() and "ACHETER" not in l.upper()), "Carte unique")
        price = parse_price(full_text)
        if price is None:
            continue
        if price < min_price:
            min_price, min_title = price, title_guess
    return (None, None) if min_price == math.inf else (min_title, min_price)

def main():
    global best_seen_price, best_seen_title
    print("[START] Surveillance Altered (Unique, prix asc)")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR")
        page = context.new_page()
        while True:
            try:
                page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
                title, price = find_min_price_card(page)
                if price is not None:
                    print(f"[INFO] Min courant: {price:.2f} € — {title}")
                    if price < best_seen_price - 1e-6:
                        print(f"[ALERT] Nouveau plus bas {price:.2f} € (ancien {best_seen_price if best_seen_price < math.inf else '∞'})")
                        send_ifttt_push(title or "Carte unique", price, TARGET_URL)
                        best_seen_price, best_seen_title = price, title
                else:
                    print("[INFO] Aucun prix détecté.")
            except PWTimeout:
                print("[WARN] Timeout chargement page.")
            except Exception as e:
                print(f"[ERR] Boucle: {e}")
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
