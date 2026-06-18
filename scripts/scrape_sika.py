"""
scrape_sika.py — Mise à jour quotidienne des cours depuis Sika Finance
=======================================================================
Récupère les cours de clôture du jour sur sikafinance.com/marches/aaz
et les ajoute à BRVM_Consolidated_Kendall_updated.xlsx (feuille Cours_Close).

Usage :
    python scrape_sika.py              # mise à jour normale + affichage
    python scrape_sika.py --dry-run    # simulation sans écriture
    python scrape_sika.py --date 2026-05-23   # forcer une date précise

Puis enchaîner avec :
    python calc_nav.py
"""

import sys, io, os, re, json, argparse, shutil, warnings
warnings.filterwarnings('ignore')

import requests
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
from openpyxl import load_workbook
from openpyxl.utils.dataframe import dataframe_to_rows

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR   = os.path.normpath(os.path.join(_SCRIPTS_DIR, "..", "data"))
ROOT_DIR   = os.path.normpath(os.path.join(_SCRIPTS_DIR, ".."))
PRIX_FILE  = os.path.join(ROOT_DIR, 'BRVM_Consolidated_Kendall_updated.xlsx')
PRIX_SHEET = '📈 Cours_Close'
SIKA_URL   = 'https://sikafinance.com/marches/aaz'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'fr-FR,fr;q=0.9,en;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Scraping Sika Finance
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_html(url: str, timeout: int = 20) -> str:
    # verify=False nécessaire dans les environnements avec proxy d'entreprise
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
    resp.raise_for_status()
    return resp.text


def _ticker_from_href(href: str) -> str | None:
    """Extrait le ticker depuis un href type '/marches/cotation_SNTS.ci'."""
    m = re.search(r'cotation_([A-Z0-9]+)', href, re.IGNORECASE)
    if not m:
        return None
    code = m.group(1).upper()
    # Enlever le suffixe pays s'il est collé (ex: SNTSci → SNTS) — ne devrait pas arriver
    return code


BRVM30_HIST_URL  = 'https://www.sikafinance.com/marches/historiques/BRVM30'
BRVM30_HIST_FILE = os.path.join(BASE_DIR, 'brvm30_index_history.json')


def scrape_brvm30_history(max_rows: int = 500) -> dict:
    """
    Scrape la page historiques/BRVM30 et retourne {date_iso: close_value}.
    Date format sur Sika : DD/MM/YYYY → converti en YYYY-MM-DD.
    """
    html = _fetch_html(BRVM30_HIST_URL)
    soup = BeautifulSoup(html, 'html.parser')
    result = {}

    # Trouver le tableau principal (le plus grand avec des dates)
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        for row in rows[1:max_rows]:   # ignorer header
            cells = [td.get_text(strip=True).replace('\xa0', '').replace(' ', '') for td in row.find_all(['td', 'th'])]
            if len(cells) < 2:
                continue
            # Colonne 0 : date (format DD/MM/YYYY)
            date_txt = cells[0]
            m = re.match(r'(\d{2})/(\d{2})/(\d{4})', date_txt)
            if not m:
                continue
            date_iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            # Colonne 1 : clôture
            close_txt = cells[1].replace(',', '.')
            try:
                close_val = float(close_txt)
                if 50 < close_val < 5000:
                    result[date_iso] = round(close_val, 4)
            except ValueError:
                continue
        if result:
            break   # on a trouvé le bon tableau

    return result


def save_brvm30_index(value: float, trade_date: 'date') -> None:
    """Sauvegarde/met à jour une valeur de l'indice BRVM30 dans brvm30_index_history.json."""
    hist = {}
    if os.path.exists(BRVM30_HIST_FILE):
        with open(BRVM30_HIST_FILE, encoding='utf-8') as f:
            try:
                hist = json.load(f)
            except Exception:
                hist = {}
    hist[str(trade_date)] = round(value, 4)
    with open(BRVM30_HIST_FILE, 'w', encoding='utf-8') as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def scrape_brvm30_index(html: str) -> float | None:
    """
    Extrait la valeur courante de l'indice BRVM30 depuis la page aaz.
    Retourne None si non trouvé.
    """
    soup = BeautifulSoup(html, 'html.parser')
    for a in soup.find_all('a', href=re.compile(r'/marches/cotation_BRVM30', re.I)):
        row = a.find_parent('tr')
        if not row:
            continue
        cells = row.find_all(['td', 'th'])
        for cell in reversed(cells):
            txt = cell.get_text(strip=True).replace('\xa0', '').replace(' ', '').replace(',', '.')
            try:
                val = float(txt)
                if 50 < val < 5000:
                    return val
            except ValueError:
                continue
    return None


def scrape_prices(html: str) -> pd.Series:
    """
    Parse la page aaz et renvoie une Series {ticker: prix_cloture}.
    Stratégie 1 : BeautifulSoup (extrait ticker via href + prix via colonnes)
    Stratégie 2 : pandas read_html (fallback)
    """
    soup = BeautifulSoup(html, 'html.parser')

    # ── Stratégie 1 : liens cotation_ + cellules de la même ligne ─────────────
    prices = {}
    for a in soup.find_all('a', href=re.compile(r'/marches/cotation_[A-Z]', re.I)):
        ticker = _ticker_from_href(a['href'])
        if not ticker:
            continue
        # Ignorer les indices (BRVMC, SIKATR, BRVM30, etc.)
        if any(x in ticker for x in ('BRVM', 'SIKA', 'COMPO')):
            continue

        row = a.find_parent('tr')
        if not row:
            continue
        cells = row.find_all(['td', 'th'])
        if len(cells) < 4:
            continue

        # Chercher la colonne "Dernier" (dernier cours = cours de clôture)
        # Le tableau a : Nom, Ouv, +Haut, +Bas, Vol(titres), Vol(XOF), Dernier, Variation
        # → "Dernier" est en général l'avant-dernière colonne
        raw_price = None
        for cell in reversed(cells):
            txt = cell.get_text(strip=True).replace('\xa0', '').replace(' ', '')
            txt = txt.replace(',', '.').replace(' ', '')
            # On cherche un nombre (cours en FCFA, souvent entre 100 et 100000)
            try:
                val = float(txt)
                if 10 < val < 1_000_000:
                    raw_price = val
                    break
            except ValueError:
                continue

        if raw_price is not None:
            prices[ticker] = raw_price

    if len(prices) >= 10:
        print(f"  Strategie 1 (BeautifulSoup) : {len(prices)} tickers extraits")
        return pd.Series(prices)

    # ── Stratégie 2 : pandas read_html ────────────────────────────────────────
    print("  Strategie 1 insuffisante, tentative avec pandas read_html...")
    try:
        tables = pd.read_html(html)
        # Chercher la table qui a une colonne "Dernier" ou "Cours"
        target = None
        for t in tables:
            cols_lower = [str(c).lower() for c in t.columns]
            if any('dernier' in c or 'cours' in c for c in cols_lower):
                target = t
                break
        if target is None and tables:
            # Prendre la plus grande table
            target = max(tables, key=len)

        if target is not None:
            # Chercher la colonne ticker (première colonne ou colonne "Nom")
            # et la colonne prix
            col_prix = None
            for c in target.columns:
                if 'dernier' in str(c).lower() or 'cours' in str(c).lower():
                    col_prix = c
                    break
            if col_prix is None:
                col_prix = target.columns[-2]  # avant-dernière par défaut

            prices2 = {}
            for _, row in target.iterrows():
                nom = str(row.iloc[0])
                # Essayer d'extraire le ticker depuis le nom (souvent "CODE - Nom société")
                m = re.match(r'^([A-Z]{3,6})', nom)
                if m:
                    tk = m.group(1)
                    try:
                        val = float(str(row[col_prix]).replace(',', '.').replace(' ', ''))
                        if 10 < val < 1_000_000:
                            prices2[tk] = val
                    except (ValueError, KeyError):
                        pass
            if prices2:
                print(f"  Strategie 2 (read_html) : {len(prices2)} tickers extraits")
                return pd.Series(prices2)

    except Exception as e:
        print(f"  Strategie 2 echouee : {e}")

    raise RuntimeError(
        "Impossible de parser les cours depuis sikafinance.com/marches/aaz.\n"
        "La structure du site a peut-être changé. Vérifiez manuellement."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Chargement / mise à jour de l'Excel
# ─────────────────────────────────────────────────────────────────────────────

def _load_existing_prices() -> pd.DataFrame:
    xl = pd.ExcelFile(PRIX_FILE)
    prices = xl.parse(PRIX_SHEET, index_col=0, parse_dates=True)
    prices.index = pd.to_datetime(prices.index)
    prices = prices.sort_index().astype(float)
    return prices


def _last_trading_date() -> date:
    """Dernier jour de bourse (lun–ven, heure Abidjan = UTC).
    Utilise aujourd'hui si le marché est ouvert (09h-18h UTC), sinon hier.
    """
    today = datetime.utcnow().date()
    # Reculer jusqu'au vendredi si week-end
    while today.weekday() >= 5:
        today -= timedelta(days=1)
    return today


def update_excel(
    scraped: pd.Series,
    trade_date: date,
    existing: pd.DataFrame,
    dry_run: bool = False,
) -> dict:
    """
    Ajoute une nouvelle ligne de cours à l'Excel.
    Renvoie un dict de résumé.
    """
    ts = pd.Timestamp(trade_date)

    # Vérifier si la date est déjà présente
    if ts in existing.index:
        return {
            'status': 'already_exists',
            'date': str(trade_date),
            'message': f"Les cours du {trade_date} sont deja dans l'Excel.",
        }

    # Construire la nouvelle ligne
    new_row = existing.iloc[-1].copy() * np.nan   # NaN pour tout le monde
    matched, unmatched = [], []

    for ticker, price in scraped.items():
        if ticker in existing.columns:
            new_row[ticker] = price
            matched.append(ticker)
        else:
            unmatched.append(ticker)

    # Ajouter la ligne
    new_df = pd.concat([existing, pd.DataFrame([new_row], index=[ts])])
    new_df = new_df.sort_index()

    summary = {
        'status': 'ok',
        'date': str(trade_date),
        'n_matched': len(matched),
        'n_unmatched': len(unmatched),
        'n_total_scraped': len(scraped),
        'tickers_missing': unmatched[:10],
        'tickers_updated': matched,
    }

    if dry_run:
        summary['status'] = 'dry_run'
        return summary

    # Sauvegarde avant modification
    backup = PRIX_FILE.replace('.xlsx', f'_backup_{trade_date}.xlsx')
    shutil.copy2(PRIX_FILE, backup)

    # Écriture openpyxl (on remplace uniquement Cours_Close)
    wb = load_workbook(PRIX_FILE)
    if PRIX_SHEET in wb.sheetnames:
        del wb[PRIX_SHEET]

    ws = wb.create_sheet(PRIX_SHEET)
    ws.append(['Date'] + list(new_df.columns))
    for idx, row in new_df.iterrows():
        row_data = [idx.date()] + [
            float(v) if pd.notna(v) else None for v in row.values
        ]
        ws.append(row_data)

    # Replacer la feuille à sa position (index 1)
    sheet_names = wb.sheetnames
    if PRIX_SHEET in sheet_names:
        current_idx = sheet_names.index(PRIX_SHEET)
        if current_idx != 1:
            wb.move_sheet(PRIX_SHEET, offset=1 - current_idx)

    wb.save(PRIX_FILE)

    # Supprimer backup si tout s'est bien passé
    try:
        os.remove(backup)
    except OSError:
        pass

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# 3. Affichage
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(s: dict, scraped: pd.Series) -> None:
    W = 60
    print("=" * W)
    print("  Sika Finance → BRVM Consolidated")
    print("=" * W)
    print(f"  Date de cotation   : {s['date']}")
    if 'n_total_scraped' in s:
        print(f"  Tickers scrapes    : {s['n_total_scraped']}")
    if 'n_matched' in s:
        print(f"  Matches Excel      : {s['n_matched']}")

    if s['status'] == 'already_exists':
        print(f"\n  [OK] {s['message']}")
    elif s['status'] == 'dry_run':
        print(f"\n  [DRY-RUN] Aucune ecriture effectuee.")
        print(f"  Exemples de cours scraped :")
        for t, p in list(scraped.items())[:8]:
            print(f"    {t:<8} : {p:>8,.0f} FCFA")
    else:
        print(f"\n  [OK] Excel mis a jour.")
        print(f"  Relancer : python calc_nav.py")

    if s.get('tickers_missing'):
        print(f"\n  Tickers Sika non reconnus : {s['tickers_missing']}")
    print("=" * W)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

RICHBOURSE_HISTORY = os.path.join(BASE_DIR, 'richbourse_history.json')


def _fallback_from_richbourse(trade_date: date) -> pd.Series:
    """Retourne les derniers cours connus depuis richbourse_history.json."""
    if not os.path.exists(RICHBOURSE_HISTORY):
        raise FileNotFoundError("richbourse_history.json introuvable — pas de fallback possible.")
    with open(RICHBOURSE_HISTORY, encoding='utf-8') as f:
        hist = json.load(f)
    date_str = str(trade_date)
    prices = {}
    for ticker, dates_dict in hist.items():
        past = sorted(d for d in dates_dict if d <= date_str)
        if past:
            prices[ticker.upper()] = float(dates_dict[past[-1]])
    if not prices:
        raise ValueError("richbourse_history.json ne contient aucun cours utilisable.")
    return pd.Series(prices)


def run(trade_date: date | None = None, dry_run: bool = False) -> dict:
    os.chdir(BASE_DIR)

    if trade_date is None:
        trade_date = _last_trading_date()

    print(f"Scraping sikafinance.com pour le {trade_date}...")

    scraped = None
    source = 'sika'
    try:
        html   = _fetch_html(SIKA_URL)
        scraped = scrape_prices(html)
    except Exception as e:
        print(f"  [WARN] Sika Finance indisponible : {e}")
        print("  Tentative fallback Richbourse...")
        try:
            scraped = _fallback_from_richbourse(trade_date)
            source  = 'richbourse_fallback'
            print(f"  [FALLBACK] {len(scraped)} cours chargés depuis richbourse_history.json")
        except Exception as e2:
            print(f"  [ERREUR] Fallback échoué : {e2}")
            return {'status': 'network_error', 'error': str(e), 'fallback_error': str(e2)}
    print(f"  {len(scraped)} cours recuperes (source: {source})")

    existing = _load_existing_prices()
    print(f"  Excel actuel : {existing.shape[0]} dates x {existing.shape[1]} tickers")

    # ── Mettre à jour l'historique de l'indice BRVM30 ────────────────────────
    if source == 'sika':
        try:
            hist_data = scrape_brvm30_history()
            if hist_data:
                # Fusionner avec l'historique existant (ne pas écraser les données existantes)
                existing_hist = {}
                if os.path.exists(BRVM30_HIST_FILE):
                    with open(BRVM30_HIST_FILE, encoding='utf-8') as _f:
                        try:
                            existing_hist = json.load(_f)
                        except Exception:
                            existing_hist = {}
                existing_hist.update(hist_data)   # les nouvelles données écrasent si conflit
                with open(BRVM30_HIST_FILE, 'w', encoding='utf-8') as _f:
                    json.dump(existing_hist, _f, ensure_ascii=False, indent=2)
                today_val = hist_data.get(str(trade_date))
                print(f"  BRVM30 historique : {len(hist_data)} dates récupérées"
                      + (f" — aujourd'hui : {today_val}" if today_val else ""))
        except Exception as _e:
            print(f"  [WARN] BRVM30 historique non récupéré : {_e}")

    summary = update_excel(scraped, trade_date, existing, dry_run=dry_run)
    summary['source'] = source
    _print_summary(summary, scraped)
    return summary


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Scraper cours Sika Finance → Excel BRVM')
    parser.add_argument('--dry-run', action='store_true',
                        help='Simulation sans modification de l\'Excel')
    parser.add_argument('--date', type=str, default=None,
                        help='Forcer une date de cotation (format YYYY-MM-DD)')
    args = parser.parse_args()

    td = None
    if args.date:
        td = date.fromisoformat(args.date)

    run(trade_date=td, dry_run=args.dry_run)
