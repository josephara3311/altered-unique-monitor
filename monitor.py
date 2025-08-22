import os, re, time, math, traceback, requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

TARGET_URL    = os.getenv("TARGET_URL")
IFTTT_KEY     = os.getenv("IFTTT_KEY")
IFTTT_EVENT   = os.getenv("IFTTT_EVENT", "altered_min_price")
POLL_SECONDS  = int(os.getenv("POLL_SECONDS", "60"))
USER_AGENT    = os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")

best_seen_price = math.inf
best_seen_title = None

def send_ifttt_push(title, price, url):
    if not IFTTT_KEY:
        print("[WARN] IFTTT_KEY manquant", flush=True); return
    r = requests.post(
        f"https://maker.ifttt.com/trigger/{IFTTT_EVENT}/json/with/key/{IFTTT_KEY}",
        json={"value1": f"Nouvelle carte moins chère : {title}",
              "value2": f"{price:.2f} €",
              "value3": url},
        timeout=15
    )
    r.raise_for_status()
    print("[OK] Notification IFTTT envoyée.", flush=True)

def parse_price(text):
    m = re.search(r"(\d+[.,]?\d*)\s*€", text.replace("\xa0"," "))
    return float(m.group(1).replace(",", ".")) if m else None

def find_min_price_card(page):
    page.wait_for_load_state("networkidle", timeout=20000)
    cards = page.locator("div").filter(has_text=re.compile(r"ACHETER", re.I))
    count = cards.count()
    min_price, min_title = math.inf, None
    for i in range(count):
        card = cards.nth(i)
        full_text = card.inner_text().strip()
        if re.search(r"\bFoiler\b", full_text, re.I):
            continue
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        title_guess = next((l for l in lines if "À PARTIR" not in l.upper() and "ACHETER" not in l.upper()), "Carte unique")
        price = parse_price(full_text)
        if price is not None and price < min_price:
            min_price, min_title = price, title_guess
    return (None, None) if min_price == math.inf else (min_title, min_price)

def main():
    global best_seen_price, best_seen_title
    print("[START] boot worker", flush=True)
    with sync_playwright() as p:
        print("[PLAYWRIGHT] starting chromium…", flush=True)
        browser = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
        context = browser.new_context(user_agent=USER_AGENT, locale="fr-FR", storage_state="storage_state.json")
        page = context.new_page()

        while True:
            try:
                print("[LOOP] goto page…", flush=True)
                page.goto(TARGET_URL, timeout=30000, wait_until="networkidle")
                title, price = find_min_price_card(page)
                if price is not None:
                    print(f"[INFO] Min courant: {price:.2f} € — {title}", flush=True)
                    if price < best_seen_price - 1e-6:
                        print(f"[ALERT] Nouveau plus bas {price:.2f} € (ancien {best_seen_price if best_seen_price < math.inf else '∞'})", flush=True)
                        send_ifttt_push(title or "Carte unique", price, TARGET_URL)
                        best_seen_price, best_seen_title = price, title
                else:
                    print("[INFO] Aucun prix détecté.", flush=True)
            except PWTimeout:
                print("[WARN] Timeout chargement page.", flush=True)
            except Exception as e:
                print(f"[ERR] Boucle: {e}\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()

