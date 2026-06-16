"""
init_launch_open.py — Réinitialise l'ancre de lancement avec les prix d'ouverture Sika
Usage : python init_launch_open.py
"""
import sys, os, re, json, warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from bs4 import BeautifulSoup
from scrape_sika import _fetch_html, SIKA_URL, _ticker_from_href
from calc_nav import (
    _load_historical_nav, _load_last_basket, _load_prices,
    _extend_nav, MGMT_FEE_ANNUAL
)


def scrape_open_prices(html: str) -> pd.Series:
    """Retourne les prix d'ouverture Sika (colonne Ouv = cells[1])."""
    soup = BeautifulSoup(html, 'html.parser')
    prices = {}
    for a in soup.find_all('a', href=re.compile(r'/marches/cotation_[A-Z]', re.I)):
        ticker = _ticker_from_href(a['href'])
        if not ticker or any(x in ticker for x in ('BRVM', 'SIKA', 'COMPO')):
            continue
        row = a.find_parent('tr')
        if not row:
            continue
        cells = row.find_all(['td', 'th'])
        if len(cells) < 2:
            continue
        try:
            txt = cells[1].get_text(strip=True).replace('\xa0', '').replace(' ', '').replace(',', '.')
            val = float(txt)
            if 10 < val < 1_000_000:
                prices[ticker] = val
        except (ValueError, IndexError):
            continue
    print(f"  Prix d'ouverture : {len(prices)} tickers extraits")
    return pd.Series(prices)


def compute_nav_open() -> float:
    """Calcule la NAV indice en utilisant les prix d'ouverture Sika."""
    os.chdir(BASE_DIR)
    html = _fetch_html(SIKA_URL)
    open_prices = scrape_open_prices(html)

    hist_nav           = _load_historical_nav()
    rebal_date, basket = _load_last_basket()
    hist_prices        = _load_prices()

    now = pd.Timestamp.now().normalize()
    live_row = {}
    for ticker in hist_prices.columns:
        if ticker in open_prices.index:
            live_row[ticker] = open_prices[ticker]
        else:
            col = hist_prices[ticker].dropna()
            live_row[ticker] = float(col.iloc[-1]) if len(col) > 0 else np.nan

    live_df = pd.concat([
        hist_prices,
        pd.DataFrame([live_row], index=[now])
    ])
    live_df = live_df[~live_df.index.duplicated(keep='last')].sort_index()

    nav_series = _extend_nav(hist_nav, rebal_date, basket, live_df, MGMT_FEE_ANNUAL)
    return float(nav_series.iloc[-1])


if __name__ == '__main__':
    os.chdir(BASE_DIR)
    launch_file = os.path.join(BASE_DIR, 'launch_state.json')

    if not os.path.exists(launch_file):
        print("ERREUR : launch_state.json introuvable. Lancez d'abord init_launch.py")
        sys.exit(1)

    with open(launch_file, encoding='utf-8') as f:
        state = json.load(f)

    print(f"Ancre actuelle   : {state['nav_index_at_launch']:.4f}  ({state['created_at']})")
    print("Scraping prix d'ouverture Sika...")

    nav_open = compute_nav_open()
    print(f"NAV à l'ouverture : {nav_open:.4f}")

    state['nav_index_at_launch'] = round(nav_open, 6)
    state['created_at'] = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M') + ' (ouverture 09h30)'

    with open(launch_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    par = state['par_fcfa']
    print(f"\nFormule VL live :")
    print(f"  VL(t) = {par:,.0f} x (NAV_index(t) / {nav_open:.4f})")
    print(f"\nlaunsh_state.json mis à jour avec l'ancre d'ouverture.")
