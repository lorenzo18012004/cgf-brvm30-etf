"""
fetch_brvm_composition_pdf.py — Détection et parsing automatique des PDFs BRVM30
=================================================================================
1. Scrape les pages BRVM connues + la recherche du site pour trouver le dernier PDF
2. Compare avec la dernière entrée de brvm_composition_history.json
3. Si nouveau PDF → télécharge → OCR → extrait les 30 tickers
4. Met à jour brvm_composition_latest.json et brvm_composition_history.json
5. Exit code 0 = nouveau trouvé, exit code 2 = déjà à jour

Usage :
    python scripts/fetch_brvm_composition_pdf.py
    python scripts/fetch_brvm_composition_pdf.py --force
"""
import os, sys, re, json, argparse, warnings, datetime
warnings.filterwarnings("ignore")

import requests
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings()

from base import BaseScript

# ── Tickers valides BRVM (source : country_map de scrape_sika_history.py) ────
KNOWN_TICKERS = {
    "ABJC","BICB","BICC","BNBC","BOAB","BOABF","BOAC","BOAM","BOAN","BOAS",
    "CABC","CBIBF","CFAC","CIEC","ECOC","ETIT","FTSC","LNBB","NEIC","NSBC",
    "NTLC","ONTBF","ORAC","ORGT","PALC","PRSC","SAFC","SCRC","SDCC","SDSC",
    "SEMC","SGBC","SHEC","SIBC","SICC","SIVC","SLBC","SMBC","SNTS","SOGC",
    "SPHC","STAC","STBC","TTLC","TTLS","UNLC","UNXC",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

# Pages BRVM à scraper (ordre de priorité — la plus récente d'abord)
ARTICLE_URLS = [
    "https://www.brvm.org/fr/avis-composition-de-lindice-brvm-30",
    "https://www.brvm.org/fr/brvm-composition-de-lindice-brvm-30-3",
    "https://www.brvm.org/fr/brvm-composition-de-lindice-brvm-30-2",
    "https://www.brvm.org/fr/brvm-composition-de-lindice-brvm-30-1",
    "https://www.brvm.org/fr/brvm-composition-de-lindice-brvm-30",
    "https://www.brvm.org/fr/brvm-nouvelle-composition-de-lindice-brvm-30-6",
    "https://www.brvm.org/fr/brvm-nouvelle-composition-de-lindice-brvm-30-5",
    "https://www.brvm.org/fr/brvm-nouvelle-composition-de-lindice-brvm-30-4",
]
SEARCH_URL = "https://www.brvm.org/fr/search?keys=composition+brvm+30"
PDF_BASE   = "https://www.brvm.org/sites/default/files/"


class BRVMCompositionPDFFetcher(BaseScript):

    def _get(self, url):
        r = requests.get(url, headers=HEADERS, timeout=25, verify=False)
        r.raise_for_status()
        return r.text

    # ------------------------------------------------------------------ #
    # Détection du dernier PDF sur le site BRVM                            #
    # ------------------------------------------------------------------ #

    def _find_pdf_links(self, html):
        """Extrait les liens PDF de composition depuis une page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        found = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "composition_de_lindice_brvm_30" in href and href.endswith(".pdf"):
                if href.startswith("http"):
                    found.append(href)
                else:
                    found.append(PDF_BASE + href.split("/")[-1])
        return found

    def _slug_to_date(self, slug):
        """Extrait la date d'un slug PDF, ex: 20260701_-_avis_... → 2026-07-01."""
        m = re.match(r"(\d{8})", slug)
        if m:
            s = m.group(1)
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return None

    def detect_latest_pdf(self):
        """
        Parcourt les pages BRVM et la recherche pour trouver l'URL du PDF
        le plus récent.
        Retourne (pdf_url, article_url, rebal_date) ou (None, None, None).
        """
        candidates = []

        # Scrape les pages d'articles connues
        for url in ARTICLE_URLS:
            try:
                html  = self._get(url)
                links = self._find_pdf_links(html)
                for link in links:
                    slug = link.split("/")[-1]
                    dt   = self._slug_to_date(slug)
                    if dt:
                        candidates.append((dt, link, url))
                        print(f"  [PAGE] {url} → {slug} ({dt})")
            except Exception as e:
                print(f"  [SKIP] {url}: {e}")

        # Scrape la recherche BRVM
        try:
            html = self._get(SEARCH_URL)
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "composition" in href and "brvm-30" in href:
                    full = href if href.startswith("http") else "https://www.brvm.org" + href
                    try:
                        sub  = self._get(full)
                        links = self._find_pdf_links(sub)
                        for link in links:
                            slug = link.split("/")[-1]
                            dt   = self._slug_to_date(slug)
                            if dt:
                                candidates.append((dt, link, full))
                    except Exception:
                        pass
        except Exception as e:
            print(f"  [SKIP] search: {e}")

        if not candidates:
            return None, None, None

        # Prend le plus récent
        candidates.sort(key=lambda x: x[0], reverse=True)
        rebal_date, pdf_url, article_url = candidates[0]
        return pdf_url, article_url, rebal_date

    # ------------------------------------------------------------------ #
    # OCR du PDF                                                            #
    # ------------------------------------------------------------------ #

    def _extract_tickers_ocr(self, pdf_path):
        """
        Convertit le PDF en images et applique OCR (tesseract) pour extraire
        les tickers. Retourne une liste de tickers validés.
        """
        try:
            import fitz
            import pytesseract
            from PIL import Image
            import io
        except ImportError as e:
            print(f"[ERREUR OCR] Dépendance manquante : {e}")
            print("  Installer : pip install pymupdf pytesseract pillow")
            print("  + tesseract-ocr (apt-get install tesseract-ocr tesseract-ocr-fra)")
            return []

        doc    = fitz.open(pdf_path)
        tickers = []

        for page in doc:
            mat = fitz.Matrix(2.5, 2.5)
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            text = pytesseract.image_to_string(img, lang="fra+eng",
                                               config="--psm 6 --oem 3")

            # Pattern : ligne du type "15- EVIOSYS... - SEMC"
            for line in text.splitlines():
                m = re.search(r"-\s*([A-Z]{3,5})\s*$", line.strip())
                if m:
                    tk = m.group(1)
                    if tk in KNOWN_TICKERS:
                        tickers.append(tk)

        doc.close()
        return sorted(set(tickers))

    # ------------------------------------------------------------------ #
    # Point d'entrée                                                        #
    # ------------------------------------------------------------------ #

    def run(self, force=False):
        hist = self.load_json("brvm_composition_history.json", [])
        if not isinstance(hist, list):
            hist = []

        last_date = hist[-1].get("rebal_date", "") if hist else ""

        print("[INFO] Recherche d'un nouveau PDF de composition BRVM30...")
        pdf_url, article_url, rebal_date = self.detect_latest_pdf()

        if not pdf_url:
            print("[WARN] Aucun PDF trouvé sur le site BRVM.")
            sys.exit(0)

        print(f"[INFO] PDF détecté : {pdf_url} ({rebal_date})")

        if not force and last_date and rebal_date <= last_date:
            print(f"[OK] Déjà à jour ({last_date}). Rien à faire.")
            sys.exit(2)

        # Téléchargement
        slug     = pdf_url.split("/")[-1]
        pdf_path = os.path.join(self.data_dir, "_tmp_brvm_compo.pdf")
        print(f"[INFO] Téléchargement...")
        r = requests.get(pdf_url, headers=HEADERS, verify=False, timeout=60)
        r.raise_for_status()
        with open(pdf_path, "wb") as f:
            f.write(r.content)
        print(f"[OK] PDF téléchargé ({len(r.content):,} bytes)")

        # OCR
        print("[INFO] OCR en cours...")
        tickers = self._extract_tickers_ocr(pdf_path)

        if len(tickers) < 25:
            print(f"[ERREUR] Seulement {len(tickers)} tickers extraits — OCR insuffisant.")
            print(f"  Tickers trouvés : {tickers}")
            sys.exit(1)

        print(f"[OK] {len(tickers)} tickers extraits : {tickers}")

        # Calcul entrées/sorties vs dernière composition
        prev_set  = set(hist[-1].get("composition", [])) if hist else set()
        new_set   = set(tickers)
        entries   = sorted(new_set - prev_set)
        exits     = sorted(prev_set - new_set)

        print(f"  Entrants : {entries}")
        print(f"  Sortants : {exits}")

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")

        new_entry = {
            "rebal_date":  rebal_date,
            "scrape_ts":   now_str,
            "pdf_slug":    slug,
            "pdf_url":     pdf_url,
            "article_url": article_url,
            "n_tickers":   len(tickers),
            "composition": sorted(tickers),
            "entries":     entries,
            "exits":       exits,
        }

        hist.append(new_entry)
        self.save_json("brvm_composition_history.json", hist)

        latest = {k: v for k, v in new_entry.items()}
        self.save_json("brvm_composition_latest.json", latest)

        print(f"[OK] brvm_composition_history.json et latest mis à jour.")
        # exit 0 = nouveau PDF traité → le workflow continue avec propose_rebalancing.py
        sys.exit(0)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                   help="Forcer même si PDF déjà connu")
    args = p.parse_args()
    BRVMCompositionPDFFetcher().run(force=args.force)
