import pandas as pd
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS  # Nouveau package (remplace duckduckgo_search)
import time
import random
import re

# --- CONFIGURATION ---
INPUT_SOURCE = [
    "0194252729546",
    "Logitech MX Master 3S",
    "B09XYZ1234",
    "EAN_INEXISTANT_000",
]
OUTPUT_CSV = "amazon_produits.csv"
DELAY_MIN = 2.0
DELAY_MAX = 4.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

def creer_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session

def chercher_asin_amazon(ref: str, session: requests.Session) -> str | None:
    """
    Tente de trouver un ASIN en recherchant directement la référence sur Amazon.fr.
    Retourne l'ASIN ou None si non trouvé / bloqué.
    """
    url = "https://www.amazon.fr/s"
    params = {"k": ref}
    try:
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        resp = session.get(url, params=params, timeout=20)

        # Détection d'un blocage CAPTCHA / page vide
        if resp.status_code in (403, 429, 503):
            return None
        if "captcha" in resp.text.lower() or "api-services-support@amazon.com" in resp.text:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        # Les résultats Amazon utilisent des attributs data-asin
        for div in soup.select("div[data-asin]"):
            asin = div.get("data-asin")
            if asin and re.fullmatch(r"[A-Z0-9]{10}", asin):
                return asin
        return None
    except requests.RequestException:
        return None

def chercher_asin_ddgs(ref: str) -> str | None:
    """
    Fallback via ddgs (DuckDuckGo) si la recherche directe Amazon échoue.
    """
    try:
        query = f'site:amazon.fr "{ref}"'
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    except Exception:
        return None

    for res in results:
        href = res.get("href", "")
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", href)
        if match:
            return match.group(1)
    return None

def extraire_asin(ref: str, session: requests.Session) -> str | None:
    """
    Combine les stratégies : ASIN direct, recherche Amazon, puis ddgs.
    """
    # 1. ASIN direct ?
    if re.fullmatch(r"[A-Z0-9]{10}", ref, re.IGNORECASE):
        return ref.upper()

    # 2. Recherche directe Amazon
    asin = chercher_asin_amazon(ref, session)
    if asin:
        return asin

    # 3. Fallback ddgs
    return chercher_asin_ddgs(ref)

def scraper_produit(asin: str, session: requests.Session) -> dict | None:
    url = f"https://www.amazon.fr/dp/{asin}"
    try:
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        resp = session.get(url, timeout=20)
        if resp.status_code in (403, 429, 503) or "captcha" in resp.text.lower():
            return None
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        titre_el = soup.select_one("#productTitle")
        titre = titre_el.get_text(strip=True) if titre_el else "Titre non disponible"

        prix = None
        prix_el = soup.select_one("#corePrice_feature_div .a-offscreen, .a-price .a-offscreen")
        if prix_el:
            txt = prix_el.get_text(strip=True).replace("€", "").replace("EUR", "").replace("\xa0", "").replace(",", ".")
            m = re.search(r"(\d+(?:\.\d{1,2})?)", txt)
            if m:
                prix = float(m.group(1))

        return {"ASIN": asin, "Titre": titre, "Prix (€)": prix, "URL": url}
    except Exception:
        return None

def traiter_references(references: list, session: requests.Session) -> pd.DataFrame:
    resultats = []
    for ref in references:
        ref = str(ref).strip()
        if not ref:
            continue
        print(f"→ Traitement de : {ref}")

        asin = extraire_asin(ref, session)
        if not asin:
            resultats.append({
                "Référence d'entrée": ref, "ASIN": "—", "Titre": "Non trouvé",
                "Prix (€)": "—", "URL": "—",
            })
            print("  ✗ ASIN non trouvé.")
            continue

        data = scraper_produit(asin, session)
        if data is None:
            resultats.append({
                "Référence d'entrée": ref, "ASIN": asin, "Titre": "Erreur de scraping",
                "Prix (€)": "—", "URL": f"https://www.amazon.fr/dp/{asin}",
            })
            print(f"  ✗ Échec du scraping pour {asin}.")
        else:
            resultats.append({
                "Référence d'entrée": ref, "ASIN": data["ASIN"], "Titre": data["Titre"],
                "Prix (€)": data["Prix (€)"] if data["Prix (€)"] is not None else "Prix non trouvé",
                "URL": data["URL"],
            })
            print(f"  ✓ OK (ASIN: {asin})")

    return pd.DataFrame(resultats)

def afficher_et_exporter(df: pd.DataFrame, chemin_csv: str) -> None:
    if df.empty:
        print("Aucun résultat.")
        return
    print("\n" + "=" * 80)
    with pd.option_context("display.max_rows", None, "display.max_columns", None, "display.width", 2000, "display.max_colwidth", 60):
        print(df.to_string(index=False))
    print("=" * 80 + "\n")
    try:
        df.to_csv(chemin_csv, index=False, encoding="utf-8-sig")
        print(f"💾 Résultats sauvegardés dans '{chemin_csv}'.")
    except Exception as e:
        print(f"❌ Erreur export CSV : {e}")

# --- POINT D'ENTRÉE ---
if __name__ == "__main__":
    session = creer_session()
    df_final = traiter_references(INPUT_SOURCE, session)
    afficher_et_exporter(df_final, OUTPUT_CSV)
