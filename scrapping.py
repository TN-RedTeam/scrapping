"""
Scraper de prix Amazon France avec Playwright (mode asynchrone).

Objectif : automatiser la veille tarifaire sur Amazon.fr à partir d'une liste
d'EAN / ASIN / mots-clés, sans API payante, avec des techniques anti-détection
raisonnables et une gestion manuelle des CAPTCHA.

⚠️  AVERTISSEMENT
- Amazon dispose d'une protection anti-bot très agressive. Même avec ces
  techniques, des blocages (CAPTCHA, page "Robot Check") restent possibles.
  Utilisez de faibles volumes et des délais généreux.
- Le scraping d'Amazon est contraire à ses CGU. Réservez cet outil à un usage
  personnel de veille tarifaire à faible volume.

Usage :
    python scrapping.py --input references.csv
    python scrapping.py --input references.csv --headless      # sans fenêtre
    python scrapping.py --input references.csv --slow           # délais plus longs

Format du CSV d'entrée : une colonne nommée "reference" (EAN, ASIN ou mots-clés).
Si le fichier n'a pas d'en-tête, la première colonne est utilisée.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys
from pathlib import Path

import pandas as pd
from playwright.async_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

OUTPUT_CSV = "resultats_amazon.csv"
BASE_URL = "https://www.amazon.fr"

# Délais aléatoires (secondes) entre deux produits pour imiter un humain.
DELAY_MIN = 3.0
DELAY_MAX = 7.0

# Un pool de User-Agents réalistes et récents (Chrome desktop, Windows/Mac).
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

# Arguments de lancement Chromium pour masquer l'automatisation.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--start-maximized",
]

# Script injecté avant tout chargement de page pour neutraliser les signaux
# classiques de détection (navigator.webdriver, chrome runtime, etc.).
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['fr-FR', 'fr', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters)
);
"""

# Sélecteurs CSS robustes pour le prix de la Buy Box, testés par ordre de priorité.
PRICE_SELECTORS = [
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#buybox .a-price .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "span.a-price span.a-offscreen",
]

# Signaux d'une page de blocage / CAPTCHA Amazon.
CAPTCHA_MARKERS = [
    "api-services-support@amazon.com",
    "Saisissez les caractères",
    "Enter the characters you see below",
    "Tapez les caractères",
    "/errors/validateCaptcha",
    "Robot Check",
]


# --------------------------------------------------------------------------- #
# UTILITAIRES
# --------------------------------------------------------------------------- #

def pause() -> float:
    """Délai aléatoire humain (asyncio) entre deux produits."""
    return random.uniform(DELAY_MIN, DELAY_MAX)


def nettoyer_prix(texte: str) -> float | None:
    """Convertit '1 299,99 €' / '19,90\xa0€' en float 1299.99 / 19.90."""
    if not texte:
        return None
    txt = (
        texte.replace("\xa0", "")
        .replace(" ", "")
        .replace("€", "")
        .replace("EUR", "")
        .strip()
    )
    # Format français : virgule décimale, point séparateur de milliers.
    txt = txt.replace(".", "").replace(",", ".")
    m = re.search(r"\d+(?:\.\d{1,2})?", txt)
    return float(m.group(0)) if m else None


def est_asin(ref: str) -> bool:
    """Un ASIN Amazon = 10 caractères alphanumériques (commence souvent par B)."""
    return bool(re.fullmatch(r"[A-Z0-9]{10}", ref, re.IGNORECASE))


def charger_references(chemin: str) -> list[str]:
    """
    Lit les références depuis un CSV. Cherche une colonne 'reference',
    sinon prend la première colonne. Tolère un fichier sans en-tête.
    """
    path = Path(chemin)
    if not path.exists():
        print(f"❌ Fichier introuvable : {chemin}")
        sys.exit(1)

    df = pd.read_csv(path, dtype=str).fillna("")
    colonnes_norm = {c.lower().strip(): c for c in df.columns}

    if "reference" in colonnes_norm:
        serie = df[colonnes_norm["reference"]]
    elif "ean" in colonnes_norm:
        serie = df[colonnes_norm["ean"]]
    else:
        # Si la 1re "colonne" ressemble déjà à une donnée (pas d'en-tête),
        # on relit sans header.
        premiere = str(df.columns[0])
        if est_asin(premiere) or premiere.isdigit():
            df = pd.read_csv(path, dtype=str, header=None).fillna("")
        serie = df.iloc[:, 0]

    refs = [str(v).strip() for v in serie if str(v).strip()]
    return refs


# --------------------------------------------------------------------------- #
# GESTION DU NAVIGATEUR
# --------------------------------------------------------------------------- #

async def creer_contexte(playwright, headless: bool):
    """Lance Chromium et crée un contexte furtif (anti-détection)."""
    browser: Browser = await playwright.chromium.launch(
        headless=headless,
        args=LAUNCH_ARGS,
    )
    context = await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        locale="fr-FR",
        timezone_id="Europe/Paris",
        viewport={"width": 1366, "height": 768},
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    # Injection du script furtif avant chaque navigation.
    await context.add_init_script(STEALTH_JS)
    return browser, context


async def gerer_cookies(page: Page) -> None:
    """Accepte la bannière de cookies Amazon si elle apparaît."""
    for selecteur in ("#sp-cc-accept", "input[name='accept']", "#a-autoid-0"):
        try:
            bouton = page.locator(selecteur)
            if await bouton.count() > 0:
                await bouton.first.click(timeout=3000)
                await page.wait_for_timeout(500)
                return
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue


async def page_bloquee(page: Page) -> bool:
    """Détecte une page CAPTCHA / blocage à partir de son contenu."""
    try:
        contenu = await page.content()
    except Exception:
        return False
    return any(marqueur in contenu for marqueur in CAPTCHA_MARKERS)


async def attendre_resolution_captcha(page: Page, headless: bool) -> bool:
    """
    En cas de CAPTCHA :
    - mode fenêtré : demande à l'utilisateur de le résoudre à la main ;
    - mode headless : impossible, on signale et on abandonne ce produit.
    Retourne True si la page n'est plus bloquée.
    """
    print("\n" + "⚠️ " * 10)
    print("🛑 CAPTCHA / blocage Amazon détecté !")
    if headless:
        print("   Mode headless : impossible de résoudre manuellement.")
        print("   → Relancez SANS --headless pour résoudre le CAPTCHA à la main.")
        print("⚠️ " * 10 + "\n")
        return False

    print("   → Résolvez le CAPTCHA dans la fenêtre du navigateur,")
    print("     puis revenez ici et appuyez sur [Entrée] pour continuer...")
    print("⚠️ " * 10 + "\n")
    # input() est bloquant ; on le délègue à un thread pour ne pas figer l'event loop.
    await asyncio.get_event_loop().run_in_executor(None, input)
    return not await page_bloquee(page)


async def comportement_humain(page: Page) -> None:
    """Petits mouvements de souris et scrolls légers pour paraître humain."""
    try:
        largeur = random.randint(200, 1000)
        hauteur = random.randint(150, 500)
        await page.mouse.move(largeur, hauteur, steps=random.randint(5, 15))
        await page.wait_for_timeout(random.randint(300, 900))
        await page.mouse.wheel(0, random.randint(300, 800))
        await page.wait_for_timeout(random.randint(400, 1200))
        await page.mouse.wheel(0, random.randint(-200, -50))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# RECHERCHE & EXTRACTION
# --------------------------------------------------------------------------- #

async def resoudre_asin(page: Page, ref: str, headless: bool) -> str | None:
    """
    Transforme une référence en ASIN :
    - si c'est déjà un ASIN, on le renvoie ;
    - sinon on lance une recherche Amazon et on récupère le 1er ASIN plausible.
    """
    if est_asin(ref):
        return ref.upper()

    url = f"{BASE_URL}/s?k={ref}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        return None

    await gerer_cookies(page)
    await comportement_humain(page)

    if await page_bloquee(page):
        if not await attendre_resolution_captcha(page, headless):
            return None

    # Chaque résultat porte un attribut data-asin ; on prend le premier valide.
    try:
        resultats = page.locator("div.s-result-item[data-asin]")
        total = await resultats.count()
        for i in range(min(total, 10)):
            asin = await resultats.nth(i).get_attribute("data-asin")
            if asin and est_asin(asin):
                return asin.upper()
    except Exception:
        return None
    return None


async def extraire_produit(page: Page, asin: str, headless: bool) -> dict:
    """Ouvre la fiche produit et extrait titre + prix de la Buy Box."""
    url = f"{BASE_URL}/dp/{asin}"
    resultat = {"ASIN": asin, "Titre": "Non trouvé", "Prix (€)": None, "URL": url}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        resultat["Titre"] = "Timeout de chargement"
        return resultat

    await gerer_cookies(page)
    await comportement_humain(page)

    if await page_bloquee(page):
        if not await attendre_resolution_captcha(page, headless):
            resultat["Titre"] = "Bloqué (CAPTCHA)"
            return resultat

    # --- Titre ---
    try:
        titre_loc = page.locator("#productTitle")
        if await titre_loc.count() > 0:
            resultat["Titre"] = (await titre_loc.first.inner_text()).strip()
    except Exception:
        pass

    # --- Prix (Buy Box), en testant les sélecteurs par ordre de priorité ---
    for selecteur in PRICE_SELECTORS:
        try:
            loc = page.locator(selecteur)
            if await loc.count() > 0:
                texte = await loc.first.inner_text()
                prix = nettoyer_prix(texte)
                if prix is not None:
                    resultat["Prix (€)"] = prix
                    break
        except Exception:
            continue

    return resultat


# --------------------------------------------------------------------------- #
# ORCHESTRATION
# --------------------------------------------------------------------------- #

async def traiter_references(references: list[str], headless: bool) -> pd.DataFrame:
    lignes: list[dict] = []

    async with async_playwright() as playwright:
        browser, context = await creer_contexte(playwright, headless)
        page = await context.new_page()

        total = len(references)
        for index, ref in enumerate(references, start=1):
            print(f"\n[{index}/{total}] → Traitement de : {ref}")

            try:
                asin = await resoudre_asin(page, ref, headless)
                if not asin:
                    print("  ✗ ASIN non trouvé.")
                    lignes.append({
                        "Référence": ref, "ASIN": "—", "Titre": "Non trouvé",
                        "Prix (€)": None, "URL": "—",
                    })
                else:
                    data = await extraire_produit(page, asin, headless)
                    prix_affiche = data["Prix (€)"] if data["Prix (€)"] is not None else "N/A"
                    print(f"  ✓ {data['Titre'][:60]} — {prix_affiche} €")
                    lignes.append({
                        "Référence": ref,
                        "ASIN": data["ASIN"],
                        "Titre": data["Titre"],
                        "Prix (€)": data["Prix (€)"],
                        "URL": data["URL"],
                    })
            except Exception as e:  # robustesse : un échec ne doit pas tout arrêter
                print(f"  ✗ Erreur inattendue : {e}")
                lignes.append({
                    "Référence": ref, "ASIN": "—", "Titre": f"Erreur : {e}",
                    "Prix (€)": None, "URL": "—",
                })

            # Délai humain avant la référence suivante (sauf après la dernière).
            if index < total:
                delai = pause()
                print(f"  ⏳ Pause de {delai:.1f}s...")
                await asyncio.sleep(delai)

        await context.close()
        await browser.close()

    return pd.DataFrame(lignes)


def afficher_et_exporter(df: pd.DataFrame, chemin_csv: str) -> None:
    if df.empty:
        print("\nAucun résultat à afficher.")
        return

    print("\n" + "=" * 90)
    print("📊  RÉCAPITULATIF DES PRIX AMAZON")
    print("=" * 90)
    with pd.option_context(
        "display.max_rows", None,
        "display.max_columns", None,
        "display.width", 2000,
        "display.max_colwidth", 55,
    ):
        print(df.to_string(index=False))
    print("=" * 90)

    try:
        df.to_csv(chemin_csv, index=False, encoding="utf-8-sig")
        print(f"\n💾 Résultats sauvegardés dans « {chemin_csv} ».")
    except Exception as e:
        print(f"\n❌ Erreur lors de l'export CSV : {e}")


# --------------------------------------------------------------------------- #
# POINT D'ENTRÉE
# --------------------------------------------------------------------------- #

def parser_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scraper de prix Amazon France avec Playwright.",
    )
    parser.add_argument(
        "--input", "-i", default="references.csv",
        help="Fichier CSV d'entrée (colonne 'reference'). Défaut : references.csv",
    )
    parser.add_argument(
        "--output", "-o", default=OUTPUT_CSV,
        help=f"Fichier CSV de sortie. Défaut : {OUTPUT_CSV}",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Lance sans fenêtre (⚠️ impossible de résoudre un CAPTCHA à la main).",
    )
    parser.add_argument(
        "--slow", action="store_true",
        help="Augmente les délais (8-15 s) pour réduire le risque de blocage.",
    )
    return parser.parse_args()


def main() -> None:
    global DELAY_MIN, DELAY_MAX
    args = parser_args()

    if args.slow:
        DELAY_MIN, DELAY_MAX = 8.0, 15.0

    references = charger_references(args.input)
    if not references:
        print("❌ Aucune référence trouvée dans le fichier d'entrée.")
        sys.exit(1)

    print(f"🚀 Démarrage du scraping de {len(references)} référence(s)...")
    print(f"   Mode : {'headless' if args.headless else 'fenêtré'} | "
          f"Délais : {DELAY_MIN:.0f}-{DELAY_MAX:.0f}s\n")

    df = asyncio.run(traiter_references(references, args.headless))
    afficher_et_exporter(df, args.output)


if __name__ == "__main__":
    main()
