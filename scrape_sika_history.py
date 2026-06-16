"""
scrape_sika_history.py — Historique complet BRVM depuis Sika Finance
=====================================================================
Scrape l'API /api/general/GetHistos par chunks de 90j depuis 2005.

Usage:
  python scrape_sika_history.py              # mise à jour (nouvelles données seulement)
  python scrape_sika_history.py --full       # re-scrape tout depuis 2005
  python scrape_sika_history.py --ticker SNTS  # un seul ticker
  python scrape_sika_history.py --since 2024-01-01  # depuis une date précise
"""
import sys, io, os, json, time, argparse, warnings
from datetime import date, timedelta
warnings.filterwarnings('ignore')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE_DIR, 'sika_history.json')
API_URL  = 'https://www.sikafinance.com/api/general/GetHistos'
START_DATE = date(2005, 1, 1)

HEADERS = {
    'User-Agent':       'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Content-Type':     'application/json;charset=UTF-8',
    'Accept':           'application/json, text/javascript, */*; q=0.01',
    'X-Requested-With': 'XMLHttpRequest',
    'Origin':           'https://www.sikafinance.com',
    'Referer':          'https://www.sikafinance.com/marches/historiques/',
}

# Suffixes pays par ticker (même table que scrape_sika_caps.py)
COUNTRY_MAP = {
    'ABJC': 'ci', 'BICB': 'bj', 'BICC': 'ci', 'BNBC': 'ci',
    'BOAB': 'bj', 'BOABF': 'bf', 'BOAC': 'ci', 'BOAM': 'ml',
    'BOAN': 'ne', 'BOAS': 'sn', 'CABC': 'ci', 'CBIBF': 'bf',
    'CFAC': 'ci', 'CIEC': 'ci', 'ECOC': 'ci', 'ETIT': 'tg',
    'FTSC': 'ci', 'LNBB': 'bj', 'NEIC': 'ci', 'NSBC': 'ci',
    'NTLC': 'ci', 'ONTBF': 'bf', 'ORAC': 'ci', 'ORGT': 'tg',
    'PALC': 'ci', 'PRSC': 'ci', 'SAFC': 'ci', 'SCRC': 'ci',
    'SDCC': 'ci', 'SDSC': 'ci', 'SEMC': 'ci', 'SGBC': 'ci',
    'SHEC': 'ci', 'SIBC': 'ci', 'SICC': 'ci', 'SIVC': 'ci',
    'SLBC': 'ci', 'SMBC': 'ci', 'SNTS': 'sn', 'SOGC': 'ci',
    'SPHC': 'ci', 'STAC': 'ci', 'STBC': 'ci', 'TTLC': 'ci',
    'TTLS': 'sn', 'UNLC': 'ci', 'UNXC': 'ci',
}


def _fetch_chunk(ticker_full: str, d_from: date, d_to: date,
                 retries: int = 3) -> list:
    """Appel API pour un chunk de max 90j. Retourne une liste de dicts."""
    payload = {
        'ticker':  ticker_full,
        'datedeb': d_from.strftime('%d/%m/%Y'),
        'datefin': d_to.strftime('%d/%m/%Y'),
        'xperiod': 0,
    }
    for attempt in range(retries):
        try:
            r = requests.post(API_URL, headers=HEADERS, json=payload,
                              verify=False, timeout=20)
            if r.status_code != 200:
                time.sleep(2)
                continue
            resp = r.json()
            err  = resp.get('error', '')
            if err == 'toolong':
                return []
            if err in ('baddt', 'nodata'):
                return []
            lst = resp.get('lst', [])
            return lst if isinstance(lst, list) else []
        except Exception:
            time.sleep(2)
    return []


def _chunks(start: date, end: date, days: int = 88):
    """Génère des tuples (debut, fin) par tranches de `days` jours."""
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=days), end)
        cur += timedelta(days=days + 1)


def scrape_ticker(ticker: str, since: date | None = None,
                  delay: float = 0.4) -> dict:
    """
    Scrape tout l'historique d'un ticker depuis `since` (ou START_DATE).
    Retourne un dict {date_iso: {close, volume, open, high, low}}.
    """
    pays = COUNTRY_MAP.get(ticker)
    if pays is None:
        print(f"  {ticker}: pays inconnu, ignoré")
        return {}

    ticker_full = f'{ticker}.{pays}'
    start = since or START_DATE
    today = date.today()
    result: dict = {}
    n_chunks = 0

    for d_from, d_to in _chunks(start, today):
        rows = _fetch_chunk(ticker_full, d_from, d_to)
        for row in rows:
            raw_date = row.get('Date', '')
            # Format DD/MM/YYYY → YYYY-MM-DD
            parts = raw_date.split('/')
            if len(parts) != 3:
                continue
            date_iso = f'{parts[2]}-{parts[1]}-{parts[0]}'
            result[date_iso] = {
                'close':  row.get('Close',  0),
                'volume': row.get('Volume', 0),
                'open':   row.get('Open',   0),
                'high':   row.get('High',   0),
                'low':    row.get('Low',    0),
            }
        n_chunks += 1
        time.sleep(delay)

    return result


def load_existing() -> dict:
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding='utf-8') as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}


def save(data: dict) -> None:
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full',   action='store_true', help='Re-scrape tout depuis 2005')
    parser.add_argument('--ticker', type=str, default=None, help='Un seul ticker')
    parser.add_argument('--since',  type=str, default=None, help='Depuis date YYYY-MM-DD')
    args = parser.parse_args()

    history = load_existing()
    tickers = [args.ticker] if args.ticker else list(COUNTRY_MAP.keys())

    for tk in tickers:
        # Déterminer la date de départ
        if args.full:
            since = START_DATE
        elif args.since:
            since = date.fromisoformat(args.since)
        else:
            # Mise à jour : reprendre 5j avant la dernière date connue (overlap)
            existing = history.get(tk, {})
            if existing:
                last_date = max(existing.keys())
                since = date.fromisoformat(last_date) - timedelta(days=5)
            else:
                since = START_DATE

        print(f"  {tk:<8}  depuis {since}...", end=' ', flush=True)
        new_data = scrape_ticker(tk, since=since)

        if new_data:
            if tk not in history:
                history[tk] = {}
            history[tk].update(new_data)
            print(f"{len(new_data)} entrées  "
                  f"(total: {len(history[tk])}  "
                  f"de {min(history[tk])} à {max(history[tk])})")
        else:
            print("aucune donnée")

    save(history)
    print(f"\nSauvegardé → {OUT_FILE}")
    print(f"Tickers: {len(history)}  "
          f"Total entrées: {sum(len(v) for v in history.values())}")


if __name__ == '__main__':
    main()
