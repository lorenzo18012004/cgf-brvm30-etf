"""
calc_nav.py — Calculateur de VL quotidienne CGF BRVM30 ETF
===========================================================
Utilise :
  1. La série VL historique (backtest) depuis dashboard_data.json
  2. Le dernier rebalancement depuis rebal_detail.json
  3. Les prix disponibles dans BRVM_Consolidated_Kendall_updated.xlsx

→ Étend la VL jusqu'au dernier prix disponible
→ Exporte nav_latest.json (pour dashboard, reporting, etc.)
→ Affiche un rapport terminal avec date des données et staleness

Usage :
    python calc_nav.py
    python calc_nav.py --par 100000          # valeur faciale par part (FCFA)
    python calc_nav.py --aum 10000000000     # AUM de référence (FCFA)
    python calc_nav.py --quiet               # pas d'affichage terminal
"""

import sys, io, os, json, argparse, warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.normpath(os.path.join(_SCRIPTS_DIR, "..", "data"))
ROOT_DIR = os.path.normpath(os.path.join(_SCRIPTS_DIR, ".."))

# ── Paramètres par défaut ─────────────────────────────────────────────────────
DEFAULT_PAR_FCFA  = 100_000    # valeur faciale d'une part (FCFA)
DEFAULT_AUM_FCFA  = 5_000_000_000  # AUM de référence au lancement (FCFA)
MGMT_FEE_ANNUAL   = 0.006
PRICE_SHEET       = '📈 Cours_Close'
PRIX_FILE         = os.path.join(ROOT_DIR, 'BRVM_Consolidated_Kendall_updated.xlsx')
DASHBOARD_FILE    = os.path.join(BASE_DIR, 'dashboard_data.json')
REBAL_FILE        = os.path.join(BASE_DIR, 'rebal_detail.json')
OUTPUT_FILE       = os.path.join(BASE_DIR, 'nav_latest.json')

# ─────────────────────────────────────────────────────────────────────────────
# 1. Chargement des données
# ─────────────────────────────────────────────────────────────────────────────

def _load_historical_nav() -> pd.Series:
    """Série VL depuis dashboard_data.json, ou nav_latest.json en fallback (cloud)."""
    if os.path.exists(DASHBOARD_FILE):
        with open(DASHBOARD_FILE, encoding='utf-8') as f:
            d = json.load(f)
        pts = d.get('nav_etf', [])
        if pts:
            s = pd.Series(
                {pd.Timestamp(p[0]): p[1] for p in pts},
                name='nav_indice'
            )
            return s.sort_index()

    # Fallback cloud : construire une série minimale depuis nav_latest.json
    nav_path = os.path.join(BASE_DIR, 'nav_latest.json')
    if not os.path.exists(nav_path):
        raise FileNotFoundError("dashboard_data.json et nav_latest.json sont tous les deux absents")
    with open(nav_path, encoding='utf-8') as f:
        nl = json.load(f)
    nav_val  = nl.get('nav_indice')
    nav_date = nl.get('calc_date')
    if not nav_val or not nav_date:
        raise ValueError("nav_latest.json ne contient pas nav_indice/calc_date")
    return pd.Series({pd.Timestamp(nav_date): float(nav_val)}, name='nav_indice')


def _load_last_basket() -> tuple[pd.Timestamp, dict]:
    """Renvoie (date_rebal, {ticker: w_etf}) du dernier rebalancement effectif."""
    with open(REBAL_FILE, encoding='utf-8') as f:
        d = json.load(f)
    rebals = [r for r in d['rebalancings'] if not r.get('skipped', False) and r.get('basket')]
    if not rebals:
        raise ValueError("Aucun rebalancement effectif dans rebal_detail.json")
    last = rebals[-1]
    dt   = pd.Timestamp(last['date'])
    basket = {item['ticker']: item['w_etf'] for item in last['basket']}
    # Normaliser les poids (somme = 1)
    total = sum(basket.values())
    basket = {k: v / total for k, v in basket.items()}
    return dt, basket


def _load_prices() -> pd.DataFrame:
    """Charge la feuille Cours_Close depuis le fichier consolidé (prix bruts). Retourne DataFrame vide si absent."""
    if not os.path.exists(PRIX_FILE):
        return pd.DataFrame()
    xl = pd.ExcelFile(PRIX_FILE)
    prices = xl.parse(PRICE_SHEET, index_col=0, parse_dates=True)
    prices.index = pd.to_datetime(prices.index)
    prices = prices.sort_index().astype(float)
    return prices


def _load_prices_sika() -> pd.DataFrame | None:
    """Cours de clôture depuis sika_history.json (source principale)."""
    path = os.path.join(BASE_DIR, 'sika_history.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        sh = json.load(f)
    rows: dict[str, dict[str, float]] = {}
    for ticker, hist in sh.items():
        for date_str, vals in hist.items():
            v = vals.get('close') if isinstance(vals, dict) else vals
            if v is not None:
                try:
                    rows.setdefault(date_str, {})[ticker] = float(v)
                except (ValueError, TypeError):
                    pass
    if not rows:
        return None
    df = pd.DataFrame.from_dict(rows, orient='index')
    df.index = pd.to_datetime(df.index)
    return df.sort_index().astype(float)


def _load_prices_adj() -> pd.DataFrame | None:
    """Cours ajustés depuis richbourse_history.json — fallback uniquement."""
    rh_path = os.path.join(BASE_DIR, 'richbourse_history.json')
    if not os.path.exists(rh_path):
        return None
    with open(rh_path, encoding='utf-8') as f:
        rh = json.load(f)
    rows: dict[str, dict[str, float]] = {}
    has_adj = False
    for ticker, hist in rh.items():
        for date_str, vals in hist.items():
            if isinstance(vals, dict):
                v = vals.get('close_adj')
                if v is not None:
                    has_adj = True
                else:
                    v = vals.get('close')
            else:
                v = vals
            if v is not None:
                try:
                    rows.setdefault(date_str, {})[ticker] = float(v)
                except (ValueError, TypeError):
                    pass
    if not has_adj:
        return None
    df = pd.DataFrame.from_dict(rows, orient='index')
    df.index = pd.to_datetime(df.index)
    return df.sort_index().astype(float)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Extension de la VL depuis le dernier rebalancement
# ─────────────────────────────────────────────────────────────────────────────

def _extend_nav(
    hist_nav: pd.Series,
    rebal_date: pd.Timestamp,
    basket: dict,
    prices: pd.DataFrame,
    fee_annual: float = MGMT_FEE_ANNUAL,
) -> pd.Series:
    """
    Étend la série VL depuis rebal_date jusqu'au dernier prix disponible.
    Utilise les poids fixes du dernier rebalancement (dérive naturelle).
    """
    fee_daily = fee_annual / 252.0

    # VL de référence au jour du rebalancement
    if rebal_date in hist_nav.index:
        base_val  = hist_nav[rebal_date]
        base_date = rebal_date
    else:
        before = hist_nav[hist_nav.index <= rebal_date]
        if not before.empty:
            base_val  = before.iloc[-1]
            base_date = before.index[-1]
        else:
            # Fallback cloud : hist_nav n'a qu'un point récent (nav_latest)
            base_val  = float(hist_nav.iloc[-1])
            base_date = pd.Timestamp(hist_nav.index[-1])

    # Tickers du panier présents dans les prix
    tickers = [t for t in basket if t in prices.columns]
    missing = [t for t in basket if t not in prices.columns]
    if missing:
        print(f"  [WARN] Tickers absents des prix : {missing}")
        # Redistribuer les poids sur les tickers présents
        w_total = sum(basket[t] for t in tickers)
        weights = {t: basket[t] / w_total for t in tickers}
    else:
        weights = {t: basket[t] for t in tickers}

    # Prix depuis rebal_date
    p = prices[tickers].copy()
    p = p[p.index >= base_date]
    # Forward-fill les prix manquants (max 5 jours ouvrés)
    p = p.ffill(limit=5)
    # Rendements quotidiens
    rets = p.pct_change().fillna(0.0)

    # Calcul de la VL jour par jour
    w_arr = np.array([weights[t] for t in tickers])
    nav_ext = {}
    vl = base_val

    for dt in rets.index[1:]:   # skip first row (returns = 0)
        r_day = rets.loc[dt].values
        portfolio_ret = float(np.dot(w_arr, r_day))
        vl = vl * (1.0 + portfolio_ret) * (1.0 - fee_daily)
        nav_ext[dt] = vl

    ext_series = pd.Series(nav_ext, name='nav_indice')

    # Fusionner : historique jusqu'à rebal_date + extension
    combined = hist_nav[hist_nav.index <= base_date].copy()
    # Ajouter uniquement les dates postérieures
    new_dates = ext_series[ext_series.index > base_date]
    combined = pd.concat([combined, new_dates]).sort_index()
    combined = combined[~combined.index.duplicated(keep='first')]
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# 3. Calcul des métriques finales
# ─────────────────────────────────────────────────────────────────────────────

def _load_launch_state() -> dict | None:
    """Charge launch_state.json s'il existe."""
    path = os.path.join(BASE_DIR, 'launch_state.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _build_nav_report(
    nav: pd.Series,
    rebal_date: pd.Timestamp,
    basket: dict,
    prices: pd.DataFrame,
    par_fcfa: float,
    aum_ref_fcfa: float,
) -> dict:
    """Construit le rapport complet de VL."""
    today = pd.Timestamp.today().normalize()
    last_price_date = nav.index[-1]
    last_nav_indice = float(nav.iloc[-1])

    # Calcul de staleness
    delta_cal = (today - last_price_date).days
    biz_days_missing = np.busday_count(
        last_price_date.date(), today.date()
    )

    # ── VL live (depuis le lancement officiel) ────────────────────────────
    launch = _load_launch_state()
    if launch:
        nav_anchor    = launch['nav_index_at_launch']   # indice backtest au lancement
        par_live      = float(launch['par_fcfa'])       # 100 000 FCFA
        n_parts       = launch['n_parts']
        launch_date   = pd.Timestamp(launch['launch_date'])
        # VL live = par × (indice_actuel / indice_au_lancement)
        vl_par_part   = par_live * (last_nav_indice / nav_anchor)
        aum_actuel_mfcfa = vl_par_part * n_parts / 1_000_000
        # Perf depuis lancement
        perf_since_launch = (last_nav_indice / nav_anchor - 1)
        # Série VL live (FCFA) depuis la date de lancement
        nav_live_series = nav[nav.index >= launch_date] if launch_date in nav.index or any(nav.index >= launch_date) else pd.Series(dtype=float)
        nav_live_fcfa   = (nav_live_series / nav_anchor * par_live).round(2)
    else:
        par_live      = par_fcfa
        n_parts       = int(aum_ref_fcfa / par_fcfa)
        launch_date   = nav.index[0]
        vl_par_part   = par_fcfa * (last_nav_indice / 100.0)
        aum_actuel_mfcfa = vl_par_part * n_parts / 1_000_000
        perf_since_launch = (last_nav_indice / 100.0 - 1)
        nav_live_fcfa = pd.Series(dtype=float)

    # ── Performances ────────────────────────────────────────────────────────
    nav_jan1 = _nav_at_year_start(nav, last_price_date.year)
    perf_ytd = (last_nav_indice / nav_jan1 - 1) if nav_jan1 else None

    # Performance backtest (depuis le début de la simulation)
    perf_backtest = (last_nav_indice / 100.0 - 1)

    # Performance 3M
    dt_3m = last_price_date - pd.DateOffset(months=3)
    nav_3m = nav[nav.index <= dt_3m]
    perf_3m = (last_nav_indice / float(nav_3m.iloc[-1]) - 1) if len(nav_3m) > 0 else None

    # ── Métriques de risque ──────────────────────────────────────────────────
    daily_rets = nav.pct_change().dropna()
    vol_ann    = float(daily_rets.std() * (252 ** 0.5)) if len(daily_rets) > 20 else None

    # Max drawdown sur toute la série
    roll_max   = nav.cummax()
    drawdowns  = (nav - roll_max) / roll_max
    max_dd     = float(drawdowns.min()) if len(drawdowns) > 0 else None

    # Sharpe annualisé (taux sans risque UEMOA ≈ 3.5%)
    rf_daily   = 0.035 / 252
    sharpe     = float((daily_rets.mean() - rf_daily) / daily_rets.std() * (252 ** 0.5)) if vol_ann else None

    # Performance 1 an
    dt_1y  = last_price_date - pd.DateOffset(years=1)
    nav_1y = nav[nav.index <= dt_1y]
    perf_1y = (last_nav_indice / float(nav_1y.iloc[-1]) - 1) if len(nav_1y) > 0 else None

    # Composition du panier avec valeurs actuelles
    tickers = [t for t in basket if t in prices.columns]
    last_prices = {}
    stale_flags = {}
    for t in tickers:
        col = prices[t].dropna()
        if len(col) == 0:
            continue
        last_prices[t] = float(col.iloc[-1])
        stale_flags[t] = (last_price_date - col.index[-1]).days > 5

    basket_rows = []
    for t in sorted(basket, key=lambda x: -basket[x]):
        w = basket[t]
        pv_mfcfa = w * aum_actuel_mfcfa
        prix = last_prices.get(t)
        stale = stale_flags.get(t, True)
        basket_rows.append({
            'ticker':       t,
            'poids_pct':    round(w * 100, 2),
            'pv_mfcfa':     round(pv_mfcfa, 1),
            'dernier_prix': round(prix, 0) if prix else None,
            'prix_stale':   stale,
        })

    # Série backtest complète (indice base 100)
    nav_series = [[dt.strftime('%Y-%m-%d'), round(v, 6)]
                  for dt, v in nav.items()]

    # Série live en FCFA (depuis la date de lancement)
    nav_live_series_out = []
    if len(nav_live_fcfa) > 0:
        nav_live_series_out = [[dt.strftime('%Y-%m-%d'), round(v, 2)]
                               for dt, v in nav_live_fcfa.items()]

    return {
        'etf_name':              'CGF BRVM30 ETF',
        'calc_date':             last_price_date.strftime('%Y-%m-%d'),
        'calc_timestamp':        pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
        'data_age_cal_days':     int(delta_cal),
        'data_age_biz_days':     int(biz_days_missing),
        'stale_warning':         bool(biz_days_missing > 0),
        'last_rebal_date':       rebal_date.strftime('%Y-%m-%d'),
        'n_basket':              len(basket),

        # Lancement
        'launch_date':           launch_date.strftime('%Y-%m-%d'),
        'launched':              launch is not None,

        # VL live (depuis le lancement)
        'nav_indice':            round(last_nav_indice, 4),
        'par_fcfa':              int(par_live),
        'vl_par_part_fcfa':      round(vl_par_part, 0),
        'n_parts':               n_parts,
        'aum_mfcfa':             round(aum_actuel_mfcfa, 1),

        # Performances
        'perf_ytd':              round(perf_ytd * 100, 2) if perf_ytd is not None else None,
        'perf_3m':               round(perf_3m * 100, 2) if perf_3m is not None else None,
        'perf_1y':               round(perf_1y * 100, 2) if perf_1y is not None else None,
        'perf_since_launch':     round(perf_since_launch * 100, 2),
        'perf_backtest_total':   round(perf_backtest * 100, 2),

        # Métriques de risque
        'vol_ann_pct':           round(vol_ann * 100, 2) if vol_ann is not None else None,
        'max_drawdown_pct':      round(max_dd * 100, 2) if max_dd is not None else None,
        'sharpe_ratio':          round(sharpe, 2) if sharpe is not None else None,

        # Composition
        'basket':                basket_rows,

        # Séries
        'nav_series':            nav_series,            # backtest (indice base 100)
        'nav_live_series':       nav_live_series_out,   # live en FCFA depuis lancement
    }


def _nav_at_year_start(nav: pd.Series, year: int) -> float | None:
    pts = nav[nav.index.year == year - 1]
    if len(pts) > 0:
        return float(pts.iloc[-1])
    pts2 = nav[nav.index.year == year]
    if len(pts2) > 0:
        return float(pts2.iloc[0])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Affichage terminal
# ─────────────────────────────────────────────────────────────────────────────

def _print_report(r: dict) -> None:
    W = 70
    print("=" * W)
    print(f"  CGF BRVM30 ETF — Valeur Liquidative Indicative")
    print("=" * W)

    # Avertissement staleness
    if r['stale_warning']:
        n = r['data_age_biz_days']
        print(f"\n  [!] DONNEES VIEILLES DE {n} JOUR(S) DE BOURSE")
        print(f"      Derniers prix disponibles au : {r['calc_date']}")
        print(f"      Mettre a jour avec : python update_prices.py")
    else:
        print(f"\n  Donnees a jour au {r['calc_date']}")

    print()
    print(f"  VL par part         : {r['vl_par_part_fcfa']:>12,.0f} FCFA")
    print(f"  Indice (base 100)   : {r['nav_indice']:>12.2f}")
    print(f"  AUM indicatif       : {r['aum_mfcfa']:>12,.1f} MFCFA")
    print(f"  Nb parts            : {r['n_parts']:>12,}")
    print()
    if r.get('launched'):
        print(f"  Depuis lancement    : {_fmt_pct(r['perf_since_launch'])}  (depuis {r['launch_date']})")
    print(f"  Performance YTD     : {_fmt_pct(r['perf_ytd'])}")
    print(f"  Performance 3 mois  : {_fmt_pct(r['perf_3m'])}")
    print(f"  Track record total  : {_fmt_pct(r['perf_backtest_total'])}  (simulation depuis 2023)")
    print()
    print(f"  Dernier rebalanc.   : {r['last_rebal_date']}")
    print(f"  Nb titres panier    : {r['n_basket']}")
    print()

    # Top 10 du panier
    print(f"  {'Ticker':<8} {'Poids':>7}   {'Valeur (MFCFA)':>14}   {'Stale':>5}")
    print(f"  {'-'*8} {'-'*7}   {'-'*14}   {'-'*5}")
    for row in r['basket'][:10]:
        stale_flag = '  [!]' if row['prix_stale'] else ''
        print(f"  {row['ticker']:<8} {row['poids_pct']:>6.1f}%   {row['pv_mfcfa']:>14,.1f}  {stale_flag}")

    if len(r['basket']) > 10:
        reste_w = sum(b['poids_pct'] for b in r['basket'][10:])
        reste_v = sum(b['pv_mfcfa'] for b in r['basket'][10:])
        print(f"  {'Autres':<8} {reste_w:>6.1f}%   {reste_v:>14,.1f}")

    print()
    print(f"  Export : nav_latest.json  ({r['calc_timestamp']})")
    print("=" * W)


def _fmt_pct(v) -> str:
    if v is None:
        return '   —'
    sign = '+' if v >= 0 else ''
    return f"{sign}{v:.2f}%"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────

def run(par_fcfa: float = DEFAULT_PAR_FCFA,
        aum_fcfa: float = DEFAULT_AUM_FCFA,
        quiet: bool = False) -> dict:
    """
    Calcule la VL, exporte nav_latest.json et renvoie le rapport.
    """
    os.chdir(BASE_DIR)

    if not quiet:
        print("Chargement des donnees...")

    hist_nav    = _load_historical_nav()
    rebal_date, basket = _load_last_basket()
    prices      = _load_prices()
    prices_sika = _load_prices_sika()
    prices_adj  = _load_prices_adj()

    # Priorité : sika > richbourse_adj > Excel
    _excel_last = prices.index[-1] if not prices.empty else pd.Timestamp.min
    _sika_last  = prices_sika.index[-1] if prices_sika is not None else pd.Timestamp.min
    _adj_last   = prices_adj.index[-1]  if prices_adj  is not None else pd.Timestamp.min

    if prices_sika is not None and _sika_last >= _excel_last:
        prices_for_nav = prices_sika
        src = "cours clôture (sika_history)"
    elif prices_adj is not None and _adj_last >= _excel_last:
        prices_for_nav = prices_adj
        src = "cours ajustés (richbourse_history)"
    elif prices_sika is not None:
        prices_for_nav = prices_sika
        src = "cours clôture (sika_history)"
    elif not prices.empty:
        prices_for_nav = prices
        src = "Cours_Close (Excel)"
    else:
        raise RuntimeError("Aucune source de prix disponible (sika_history.json absent et pas d'Excel)")

    if not quiet:
        print(f"  Serie historique  : {hist_nav.index[0].date()} → {hist_nav.index[-1].date()} ({len(hist_nav)} pts)")
        print(f"  Dernier rebal     : {rebal_date.date()} ({len(basket)} titres)")
        print(f"  Prix pour VL      : {src} jusqu'au {prices_for_nav.index[-1].date()}")
        print("Extension de la serie VL...")

    nav = _extend_nav(hist_nav, rebal_date, basket, prices_for_nav)

    if not quiet:
        ext_pts = len(nav) - len(hist_nav[hist_nav.index <= rebal_date])
        print(f"  +{ext_pts} jours etendus → serie totale : {len(nav)} pts")

    report = _build_nav_report(nav, rebal_date, basket, prices, par_fcfa, aum_fcfa)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    if not quiet:
        _print_report(report)

    return report


if __name__ == '__main__':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='Calculateur VL CGF BRVM30 ETF')
    parser.add_argument('--par',   type=float, default=DEFAULT_PAR_FCFA,
                        help=f'Valeur faciale par part FCFA (défaut: {DEFAULT_PAR_FCFA:,.0f})')
    parser.add_argument('--aum',   type=float, default=DEFAULT_AUM_FCFA,
                        help=f'AUM référence FCFA (défaut: {DEFAULT_AUM_FCFA:,.0f})')
    parser.add_argument('--quiet', action='store_true', help='Pas d\'affichage terminal')
    args = parser.parse_args()

    run(par_fcfa=args.par, aum_fcfa=args.aum, quiet=args.quiet)
