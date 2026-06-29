"""
Reconstruction backtest Total Return (dividendes inclus) + tests de validation.

Méthodologie :
  - Panier : ADV-cap + redistribution (62j grands titres / 32j petits titres)
  - Participation max : 15% de l'ADV quotidien (screen trading)
    → max_w = 0.15 × ADV × max_days / AUM
  - Spread variable selon ADV : 25 bps (très liquide) → 175 bps (illiquide)
  - Dividendes : reçus ~juillet de chaque année (BRVM paie Y+1 pour exercice Y),
    capitalisés au taux sans risque 3%/an, distribués le 30 juin et 31 décembre
  - Frais de gestion : 0.60%/an (déduits quotidiennement)

Usage :  python scripts/run_backtest_validation.py
Sorties :
  data/dashboard_data.json
  data/validation_results.json
  data/scalability_results.json
  data/backtest_metrics.json
"""
import sys, os, json, random
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

# ── Chemins ───────────────────────────────────────────────────────────────────
BASE      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA      = os.path.join(BASE, 'data')
EXCEL_DIR = os.path.join(BASE, 'excel')

DD_PATH = os.path.join(DATA, 'dashboard_data.json')
BM_PATH = os.path.join(DATA, 'backtest_metrics.json')
VL_PATH = os.path.join(DATA, 'validation_results.json')
SC_PATH = os.path.join(DATA, 'scalability_results.json')
SH_PATH = os.path.join(DATA, 'sika_history.json')
RD_PATH = os.path.join(DATA, 'rebal_detail.json')
EX_PATH = os.path.join(EXCEL_DIR, 'BRVM_Consolidated_Kendall_updated.xlsx')

# ── Paramètres de sélection du panier ────────────────────────────────────────
MAX_EXEC_SMALL      = 32      # Petits titres (<LARGE_THRESHOLD) : max 32j
MAX_EXEC_LARGE      = 62      # Grands titres (>=LARGE_THRESHOLD) : max 62j (OTC)
LARGE_THRESHOLD     = 0.03    # Seuil "grand titre" : 3% du BRVM30
PARTICIPATION_RATE  = 0.20    # Max 20% de l'ADV quotidien (screen + OTC petits blocs)
MIN_ADV_MFCFA       = 0.5    # ADV minimum pour être inclus (M FCFA/j)
MIN_BASKET_WEIGHT   = 0.001   # Poids minimum après redistribution (0.1%)
FORCE_TOP_N         = 5       # Top N titres (par poids BRVM30) tenus à leur poids exact (OTC)
MGMT_FEE_ANN        = 0.006  # Frais de gestion : 0.60%/an
AUM_MFCFA           = 5_000  # AUM de référence en M FCFA (5 Md)
RF_RATE_ANN         = 0.03   # Taux sans risque annuel (UEMOA) pour placement dividendes

START_DATE = '2023-01-02'
END_DATE   = '2026-04-01'

random.seed(42)
np.random.seed(42)

# ══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT
# ══════════════════════════════════════════════════════════════════════════════
print("[1/6] Chargement des données…")
dd = json.load(open(DD_PATH, encoding='utf-8'))
sh = json.load(open(SH_PATH, encoding='utf-8'))
bm = json.load(open(BM_PATH, encoding='utf-8'))
rd = json.load(open(RD_PATH, encoding='utf-8'))

# ── Dividendes historiques ────────────────────────────────────────────────────
# dividend_history : {ticker: {year: montant_fcfa_par_action}}
# Convention BRVM : dividende versé en Y+1 pour exercice Y (ex-date ~juillet Y+1)
_dh_raw   = json.load(open(os.path.join(DATA, 'dividend_history.json'), encoding='utf-8'))
_div_hist  = _dh_raw.get('history', {})   # {ticker: {'2022': 753.0, '2023': 780.0, ...}}

# Construire un calendrier ex-dividende : {date_str: {ticker: montant_fcfa}}
# Ex-date assumée : 1er juillet de l'année de versement (Y+1)
_div_calendar = {}   # {'2023-07-01': {'ORAC': 753.0, ...}, ...}
for ticker, years in _div_hist.items():
    for year_str, amount in years.items():
        if amount is None or amount <= 0:
            continue
        try:
            pay_year = int(year_str) + 1          # versé l'année suivante
            ex_date  = f'{pay_year}-07-01'
            if ex_date not in _div_calendar:
                _div_calendar[ex_date] = {}
            _div_calendar[ex_date][ticker] = float(amount)
        except (ValueError, TypeError):
            pass
print(f"   Dividendes : {sum(len(v) for v in _div_calendar.values())} versements "
      f"sur {len(_div_calendar)} dates (2023-2026)")

w_history   = dd['w_history']
rebal_dates = sorted(w_history.keys())

# Poids BRVM30 par ticker à chaque rebalancement (depuis rebal_detail)
brvm30_weights_hist = {}   # {rebal_date: {ticker: w_brvm30}}
for r in rd.get('rebalancings', []):
    dt = r.get('date', '')
    basket   = r.get('basket',   [])
    excluded = r.get('excluded', [])
    w_map = {}
    for item in basket + excluded:
        tk = item.get('ticker')
        w  = item.get('w_brvm30', 0.0)
        if tk and w:
            w_map[tk] = w
    if w_map:
        brvm30_weights_hist[dt] = w_map

# ── BRVM30 PR depuis Sika (brvm30_index_history.json) ────────────────────────
print("[2/6] Lecture BRVM30 PR depuis Sika…")
BRVM30_PATH = os.path.join(DATA, 'brvm30_index_history.json')
_brvm30_raw = json.load(open(BRVM30_PATH, encoding='utf-8'))
brvm30_raw = {k: float(v) for k, v in _brvm30_raw.items() if v}

# Toutes les dates de trading dans sika
all_dates = sorted({d for tk in sh for d in sh[tk]
                    if START_DATE <= d <= END_DATE})

# ══════════════════════════════════════════════════════════════════════════════
# MODULE DE SÉLECTION DU PANIER
# ══════════════════════════════════════════════════════════════════════════════

def compute_adv(ticker: str, as_of_date: str, window_days: int = 63) -> float:
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window_days:]
    vals  = [(hist[d].get('volume', 0) or 0) * (hist[d].get('close', 0) or 0) / 1e6
             for d in dates]
    return float(sum(vals) / len(dates)) if dates else 0.0


def spread_one_way(adv_mfcfa: float) -> float:
    """Spread bid-ask one-way selon la liquidité du titre (en fraction, pas en bps)."""
    if adv_mfcfa >= 100: return 0.0025   # 25 bps — très liquide
    if adv_mfcfa >=  30: return 0.0040   # 40 bps
    if adv_mfcfa >=  10: return 0.0080   # 80 bps
    if adv_mfcfa >=   5: return 0.0125   # 125 bps
    return 0.0175                          # 175 bps — quasi-illiquide


def build_basket(rebal_date: str, w_brvm30: dict,
                 aum_mfcfa: float = AUM_MFCFA,
                 max_small: int = MAX_EXEC_SMALL,
                 max_large: int = MAX_EXEC_LARGE,
                 large_thr: float = LARGE_THRESHOLD,
                 force_top_n: int = FORCE_TOP_N) -> dict:
    """
    Retourne {ticker: poids_final} normalisé à 1.

    Stratégie hybride :
      - Top N titres (par poids BRVM30) : tenus à leur poids BRVM30 exact via OTC.
        Aucune contrainte ADV — la position est construite progressivement en gré-à-gré.
      - Titres restants : ADV-cap + redistribution classique (62j/32j).
        Le budget de poids disponible = 1 - somme(forced).
    """
    total_brvm30 = sum(w_brvm30.values()) or 1.0
    # Normaliser les poids BRVM30 (au cas où ils ne somment pas à 1)
    w_norm = {tk: v / total_brvm30 for tk, v in w_brvm30.items()}

    # ── Top N forcés (OTC) ───────────────────────────────────────────────────
    sorted_tks = sorted(w_norm, key=lambda x: -w_norm[x])
    forced_tks = set(sorted_tks[:force_top_n])
    rest_tks   = [tk for tk in sorted_tks if tk not in forced_tks]

    forced_w = {tk: w_norm[tk] for tk in forced_tks}
    forced_total = sum(forced_w.values())
    rest_budget  = 1.0 - forced_total   # poids disponible pour les autres

    # ── Restants : ADV-cap + redistribution ─────────────────────────────────
    adv     = {tk: compute_adv(tk, rebal_date) for tk in rest_tks}
    eligible = [tk for tk in rest_tks if adv[tk] >= MIN_ADV_MFCFA]

    if not eligible:
        return {tk: round(v, 6) for tk, v in forced_w.items()}

    # Poids cibles pour les restants, normalisés au sein du groupe puis
    # mis à l'échelle du budget disponible
    total_rest = sum(w_norm[tk] for tk in eligible) or 1.0
    weights = {tk: w_norm[tk] / total_rest * rest_budget for tk in eligible}

    # Plafond ADV (20% de l'ADV quotidien × max_days) pour les restants
    max_w = {}
    for tk in eligible:
        days = max_large if w_norm[tk] >= large_thr else max_small
        max_w[tk] = min(PARTICIPATION_RATE * adv[tk] * days / aum_mfcfa, rest_budget)

    # Itération plafonner + redistribuer
    for _ in range(50):
        capped   = {tk for tk in eligible if weights[tk] > max_w[tk]}
        uncapped = [tk for tk in eligible if tk not in capped]
        if not capped:
            break
        excess = sum(weights[tk] - max_w[tk] for tk in capped)
        for tk in capped:
            weights[tk] = max_w[tk]
        uncapped_total = sum(weights[tk] for tk in uncapped)
        if uncapped_total <= 0 or not uncapped:
            break
        for tk in uncapped:
            weights[tk] += excess * weights[tk] / uncapped_total

    # Exclure poids < 0.1%
    for _ in range(10):
        tiny = [tk for tk in eligible if 0 < weights[tk] < MIN_BASKET_WEIGHT]
        if not tiny:
            break
        for tk in tiny:
            eligible.remove(tk)
        if not eligible:
            break
        total_keep = sum(weights[tk] for tk in eligible)
        for tk in eligible:
            weights[tk] = weights[tk] / total_keep * rest_budget if total_keep > 0 else rest_budget / len(eligible)

    # ── Assemblage final ─────────────────────────────────────────────────────
    final = {**forced_w, **{tk: weights[tk] for tk in eligible if weights.get(tk, 0) > 0}}
    total = sum(final.values())
    return {tk: round(v / total, 6) for tk, v in final.items()} if total > 0 else {}


# ══════════════════════════════════════════════════════════════════════════════
# NAV PRICE RETURN (backtest principal — utilise w_history pré-calculé)
# ══════════════════════════════════════════════════════════════════════════════
print("[3/6] Reconstruction NAV Price Return…")


def _get_weights(wh, date_key):
    val = wh[date_key]
    if isinstance(val, dict):
        return val
    if isinstance(val, list) and len(val) == 2 and isinstance(val[1], dict):
        return val[1]
    return {}


_daily_rf = (1 + RF_RATE_ANN) ** (1 / 252) - 1  # rendement RF quotidien
_DIST_MMDD = {'06-30', '12-31'}                  # dates de distribution semestrielle


def build_nav_tr(all_dates, sh, rb_dates, wh,
                 fee_ann=MGMT_FEE_ANN,
                 div_cal=None,
                 adv_at_rebal=None,
                 monthly_rebal=True,
                 drift_threshold=0.01):
    """
    Retourne (nav_gross_tr, nav_net_tr, nav_dist_series).

    Rebalancement :
      - Trimestriel : mise à jour de la cible (nouvelle composition du panier)
      - Mensuel (si monthly_rebal=True) : vérification de la dérive vs cible
        → on ne trade que les titres dont |w_actuel - w_cible| > drift_threshold
        → réduit les transactions et donc les frais, tout en limitant la dérive

    Modèle de coûts :
      - Spread variable par titre selon son ADV (spread_one_way())
      - Frais de gestion déduits quotidiennement

    Modèle dividendes :
      - Ex-date ~1er juillet (Y+1 pour exercice Y)
      - Réserve capitalisée au taux RF 3%/an, distribuée dernier jour de bourse
        de juin et décembre
    """
    if div_cal is None:
        div_cal = _div_calendar
    if adv_at_rebal is None:
        adv_at_rebal = {}

    gross_pts, net_pts = {}, {}
    nav_gross  = 100.0
    nav_net    = 100.0
    _daily_fee = (1 - fee_ann) ** (1 / 252)

    div_reserve_gross = 0.0
    div_reserve_net   = 0.0
    nav_dist_series   = {}

    rb_idx       = 0
    target_w     = dict(_get_weights(wh, rb_dates[0]))
    portfolio    = dict(target_w)

    for i, dt in enumerate(all_dates):
        if i == 0:
            gross_pts[dt] = nav_gross
            net_pts[dt]   = nav_net
            continue

        prev_dt = all_dates[i - 1]

        # ── Mark-to-market ────────────────────────────────────────────────────
        total_prev    = sum(portfolio.values()) or 1.0
        new_portfolio = {}
        for tk, v in portfolio.items():
            p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
            p2 = sh.get(tk, {}).get(dt,      {}).get('close')
            new_portfolio[tk] = v * (p2 / p1) if (p1 and p2 and p1 > 0) else v
        total_new = sum(new_portfolio.values())
        r_t       = (total_new / total_prev - 1) if total_prev > 0 else 0.0
        portfolio  = new_portfolio

        nav_gross *= (1 + r_t)
        nav_net   *= (1 + r_t) * _daily_fee

        # ── Dividendes reçus ce jour ──────────────────────────────────────────
        if dt in div_cal:
            for tk, div_amount in div_cal[dt].items():
                if tk not in portfolio:
                    continue
                p_prev = sh.get(tk, {}).get(prev_dt, {}).get('close')
                if not p_prev or p_prev <= 0:
                    continue
                div_yield    = div_amount / p_prev
                frac_in_port = portfolio[tk] / (sum(portfolio.values()) or 1.0)
                div_income_gross = nav_gross * frac_in_port * div_yield
                div_income_net   = nav_net   * frac_in_port * div_yield
                div_reserve_gross += div_income_gross
                div_reserve_net   += div_income_net

        # ── Capitalisation quotidienne de la réserve au taux RF ───────────────
        div_reserve_gross *= (1 + _daily_rf)
        div_reserve_net   *= (1 + _daily_rf)

        # ── Distribution semestrielle (dernier jour de bourse de juin/décembre) ─
        cur_month  = dt[5:7]
        next_month = all_dates[i + 1][5:7] if i + 1 < len(all_dates) else ''
        if cur_month in ('06', '12') and next_month != cur_month and div_reserve_gross > 0:
            nav_dist_series[dt] = round(div_reserve_gross, 6)
            div_reserve_gross = 0.0
            div_reserve_net   = 0.0

        # ── Mise à jour de la cible (trimestrielle : nouvelle composition) ─────
        target_updated = False
        while rb_idx + 1 < len(rb_dates) and dt >= rb_dates[rb_idx + 1]:
            rb_idx       += 1
            target_w      = dict(_get_weights(wh, rb_dates[rb_idx]))
            target_updated = True

        # ── Rebalancement mensuel avec seuil de dérive ───────────────────────
        # Premier jour de bourse du mois OU mise à jour de la cible trimestrielle
        is_first_of_month = (dt[:7] != all_dates[i - 1][:7])
        if monthly_rebal and (is_first_of_month or target_updated):
            total_port  = sum(portfolio.values()) or 1.0
            curr_w_norm = {tk: v / total_port for tk, v in portfolio.items()}
            all_tks     = set(curr_w_norm) | set(target_w)

            # Titres qui ont dérivé au-delà du seuil
            drifted = {tk for tk in all_tks
                       if abs(target_w.get(tk, 0) - curr_w_norm.get(tk, 0)) > drift_threshold}

            if drifted:
                adv_rb = adv_at_rebal.get(rb_dates[rb_idx], {})
                cost_rebal = sum(
                    abs(target_w.get(t, 0) - curr_w_norm.get(t, 0)) *
                    spread_one_way(adv_rb.get(t, compute_adv(t, rb_dates[rb_idx])))
                    for t in drifted
                ) / 2

                nav_gross *= (1 - cost_rebal)
                nav_net   *= (1 - cost_rebal)

                # Ramener les titres drifted à leur cible, garder les autres
                new_w_partial = {}
                for tk in all_tks:
                    if tk in drifted:
                        new_w_partial[tk] = target_w.get(tk, 0)
                    elif tk in curr_w_norm:
                        new_w_partial[tk] = curr_w_norm[tk]
                total_partial = sum(new_w_partial.values())
                if total_partial > 0:
                    portfolio = {tk: v / total_partial
                                 for tk, v in new_w_partial.items() if v > 0}

        elif not monthly_rebal and target_updated:
            # Mode trimestriel classique : rebalancement complet à chaque cible
            all_tks     = set(portfolio) | set(target_w)
            total_port  = sum(portfolio.values()) or 1.0
            curr_w_norm = {tk: v / total_port for tk, v in portfolio.items()}
            adv_rb = adv_at_rebal.get(rb_dates[rb_idx], {})
            cost_rebal = sum(
                abs(target_w.get(t, 0) - curr_w_norm.get(t, 0)) *
                spread_one_way(adv_rb.get(t, compute_adv(t, rb_dates[rb_idx])))
                for t in all_tks
            ) / 2
            nav_gross *= (1 - cost_rebal)
            nav_net   *= (1 - cost_rebal)
            portfolio   = dict(target_w)

        gross_pts[dt] = nav_gross
        net_pts[dt]   = nav_net

    return (pd.Series(gross_pts, dtype=float),
            pd.Series(net_pts,   dtype=float),
            nav_dist_series)


# ── Pré-calcul de l'ADV par titre à chaque rebal (évite de recalculer en boucle)
_adv_at_rebal = {}
for _rb in rebal_dates:
    _adv_rb = {}
    for _r in rd.get('rebalancings', []):
        if _r.get('date') == _rb:
            for _item in _r.get('basket', []) + _r.get('excluded', []):
                if _item.get('ticker') and _item.get('adv_mfcfa'):
                    _adv_rb[_item['ticker']] = _item['adv_mfcfa']
    _adv_at_rebal[_rb] = _adv_rb

nav_gross_pr, nav_net_pr, _dist = build_nav_tr(
    all_dates, sh, rebal_dates, w_history, adv_at_rebal=_adv_at_rebal
)
# Alias pour compatibilité avec le reste du script
nav_gross_pr = nav_gross_pr   # Total Return gross
nav_net_pr   = nav_net_pr     # Total Return net
print("   NAV gross TR: %.2f → %.2f" % (nav_gross_pr.iloc[0], nav_gross_pr.iloc[-1]))
print("   NAV net   TR: %.2f → %.2f" % (nav_net_pr.iloc[0],   nav_net_pr.iloc[-1]))
if _dist:
    print("   Distributions:", {d: f"{v:.4f} pts NAV ({v/nav_gross_pr.get(d, 100)*100:.2f}% de NAV)" for d, v in sorted(_dist.items())})

# ── Exec days (delta one-way par titre) pour la TE progressive ───────────────
_adv_map = {}
for _r in rd.get('rebalancings', []):
    _dt = _r.get('date')
    _m = {}
    for _item in _r.get('basket', []) + _r.get('excluded', []):
        _tk = _item.get('ticker', '')
        _adv = _item.get('adv_mfcfa', 0)
        if _tk and _adv:
            _m[_tk] = _adv
    if _m:
        _adv_map[_dt] = _m

exec_days_map = {}
for _i, _dt in enumerate(rebal_dates):
    _new_w  = _get_weights(w_history, _dt)
    _prev_w = _get_weights(w_history, rebal_dates[_i-1]) if _i > 0 else {}
    _adv_dt = _adv_map.get(_dt, {})
    _n_max  = 1.0
    for _tk in set(_prev_w) | set(_new_w):
        _delta = abs(_new_w.get(_tk, 0) - _prev_w.get(_tk, 0))
        _adv   = _adv_dt.get(_tk, 0)
        if _adv > 0 and _delta > 0:
            _n_max = max(_n_max, (_delta * AUM_MFCFA) / _adv)
    exec_days_map[_dt] = int(np.ceil(min(_n_max, 30)))


def build_nav_pr_prog(all_dates, sh, rb_dates, wh, exec_days_map,
                      fee_ann=MGMT_FEE_ANN, cost_tx=0.005):
    """
    NAV avec exécution progressive : transition linéaire des poids sur
    exec_days_map[rb_date] jours. Coût réparti sur ces jours.
    """
    gross_pts, net_pts = {}, {}
    nav_gross = 100.0
    nav_net   = 100.0
    _daily_fee = (1 - fee_ann) ** (1 / 252)
    rb_idx    = 0
    portfolio = dict(_get_weights(wh, rb_dates[0]))
    transition = None  # {old_w, new_w, n_days, day_k, daily_cost}

    for i, dt in enumerate(all_dates):
        if i == 0:
            gross_pts[dt] = nav_gross
            net_pts[dt]   = nav_net
            continue
        prev_dt = all_dates[i - 1]

        while rb_idx + 1 < len(rb_dates) and dt >= rb_dates[rb_idx + 1]:
            rb_idx  += 1
            new_w    = _get_weights(wh, rb_dates[rb_idx])
            n_exec   = exec_days_map.get(rb_dates[rb_idx], 1)
            total_port = sum(portfolio.values()) or 1.0
            old_w    = {tk: v / total_port for tk, v in portfolio.items()}
            all_tks_r = set(old_w) | set(new_w)
            to = sum(abs(new_w.get(t, 0) - old_w.get(t, 0)) for t in all_tks_r) / 2
            transition = {'old_w': dict(old_w), 'new_w': dict(new_w),
                          'n_days': n_exec, 'day_k': 0,
                          'daily_cost': cost_tx * to / n_exec}

        if transition is not None:
            frac = min(transition['day_k'] / transition['n_days'], 1.0)
            all_tks_t = set(transition['old_w']) | set(transition['new_w'])
            eff_w = {tk: transition['old_w'].get(tk, 0) * (1 - frac) +
                         transition['new_w'].get(tk, 0) * frac
                     for tk in all_tks_t}
            cost_today = transition['daily_cost']
            transition['day_k'] += 1
            if transition['day_k'] >= transition['n_days']:
                portfolio  = dict(transition['new_w'])
                transition = None
        else:
            total_port = sum(portfolio.values()) or 1.0
            eff_w = {tk: v / total_port for tk, v in portfolio.items()}
            cost_today = 0.0

        total_eff = sum(eff_w.values()) or 1.0
        r_t = 0.0
        for tk, v in eff_w.items():
            p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
            p2 = sh.get(tk, {}).get(dt,      {}).get('close')
            if p1 and p2 and p1 > 0:
                r_t += (v / total_eff) * (p2 / p1 - 1)

        nav_gross *= (1 + r_t) * (1 - cost_today)
        nav_net   *= (1 + r_t) * (1 - cost_today) * _daily_fee

        if transition is None:
            new_portfolio = {}
            for tk, v in portfolio.items():
                p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
                p2 = sh.get(tk, {}).get(dt,      {}).get('close')
                new_portfolio[tk] = v * (p2 / p1) if (p1 and p2 and p1 > 0) else v
            portfolio = new_portfolio

        gross_pts[dt] = nav_gross
        net_pts[dt]   = nav_net
    return pd.Series(gross_pts, dtype=float), pd.Series(net_pts, dtype=float)


nav_gross_prog, nav_net_prog = build_nav_pr_prog(all_dates, sh, rebal_dates, w_history, exec_days_map)
print("   NAV progressive: %.2f → %.2f" % (nav_net_prog.iloc[0], nav_net_prog.iloc[-1]))

# ── Benchmark BRVM30 PR ───────────────────────────────────────────────────────
base_val   = brvm30_raw[START_DATE]
bench_dict = {d: v / base_val * 100 for d, v in brvm30_raw.items()}

nav_bench, last_pr = [], 100.0
for dt in all_dates:
    if dt in bench_dict:
        last_pr = bench_dict[dt]
    nav_bench.append([dt, round(last_pr, 6)])
bench_s = pd.Series({d: v for d, v in nav_bench}, dtype=float)

# ══════════════════════════════════════════════════════════════════════════════
# MÉTRIQUES GLOBALES
# ══════════════════════════════════════════════════════════════════════════════
print("[4/6] Calcul des métriques globales PR…")


def compute_te_td(nav_s, bench_s_):
    idx = nav_s.index.intersection(bench_s_.index)
    e, b = nav_s[idx], bench_s_[idx]
    if len(e) < 10:
        return 0.0, 0.0, 0.0
    r_e = e.pct_change().dropna()
    r_b = b.pct_change().dropna()
    ci  = r_e.index.intersection(r_b.index)
    te  = float((r_e[ci] - r_b[ci]).std(ddof=1) * np.sqrt(252))
    td  = float(e.iloc[-1] / e.iloc[0] / (b.iloc[-1] / b.iloc[0]) - 1)
    # Annualisation sur jours calendaires réels (convention reporting fonds)
    n_cal = (pd.Timestamp(e.index[-1]) - pd.Timestamp(e.index[0])).days
    n_y   = n_cal / 365.25 if n_cal > 0 else len(e) / 252
    td_ann = float((1 + td) ** (1 / n_y) - 1) if n_y > 0 else 0.0
    return te, td, td_ann


te_gross, td_gross, td_gross_ann = compute_te_td(nav_gross_pr, bench_s)
te_net,   td_net,   td_net_ann   = compute_te_td(nav_net_pr,   bench_s)
te_prog,  td_prog,  _            = compute_te_td(nav_net_prog,  bench_s)
print("   TE gross=%.2f%%  TD gross=%+.2f%%/an" % (te_gross*100, td_gross_ann*100))
print("   TE net=%.2f%%    TD net=%+.2f%%/an"   % (te_net*100,   td_net_ann*100))
print("   TE progressive=%.2f%%  TD progressive=%+.2f%%" % (te_prog*100, td_prog*100))


def turnover_avg(wh, rb_dates):
    tos = []
    for i in range(1, len(rb_dates)):
        wp = _get_weights(wh, rb_dates[i-1])
        wc = _get_weights(wh, rb_dates[i])
        tks = set(wp) | set(wc)
        tos.append(sum(abs(wc.get(t, 0) - wp.get(t, 0)) for t in tks) / 2)
    return float(np.mean(tos)) if tos else 0.0


to_avg = turnover_avg(w_history, rebal_dates)

# Métriques annuelles
annual_new = []
for yr in [2023, 2024, 2025, 2026]:
    mask = [d for d in nav_net_pr.index if d.startswith(str(yr))]
    e_yr = nav_net_pr[mask];  g_yr = nav_gross_pr[mask]
    b_yr = bench_s[[d for d in bench_s.index if d.startswith(str(yr))]]
    if len(e_yr) < 5:
        continue
    te_yr, td_yr, _ = compute_te_td(e_yr, b_yr)
    te_gr, td_gr, _ = compute_te_td(g_yr, b_yr)
    old = next((a for a in bm.get('annual', []) if a.get('year') == yr), {})
    annual_new.append({'year': yr, 'te': round(te_yr, 6), 'td': round(td_yr, 6),
                       'td_gross': round(td_gr, 6),
                       'cost_tx': old.get('cost_tx', 0), 'mgmt_fee': old.get('mgmt_fee', 0),
                       'basket_gap': round(td_gr, 6)})
    print("   %d: TE=%.2f%%  TD=%+.2f%%" % (yr, te_yr*100, td_yr*100))

# ── Sauvegarde dashboard_data.json ───────────────────────────────────────────
print("[5/6] Mise à jour dashboard_data.json…")
dd['nav_bench'] = nav_bench
dd['nav_etf']   = [[d, round(v, 6)] for d, v in nav_net_pr.items()]
dd['nav_gross'] = [[d, round(v, 6)] for d, v in nav_gross_pr.items()]
m = dd.get('metrics', {})
m.update({'te': round(te_net, 6), 'te_gross': round(te_gross, 6),
          'td': round(td_net, 6), 'td_ann': round(td_net_ann, 6),
          'td_gross': round(td_gross, 6), 'td_gross_ann': round(td_gross_ann, 6),
          'div_bench_factor': 1.0, 'div_etf_factor': 1.0})
dd['metrics'] = m
json.dump(dd, open(DD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))

# Mise à jour backtest_metrics.json avec les paramètres de sélection
bm.update({
    'te_full': round(te_net, 6), 'te_prog': round(te_prog, 6),
    'td_full': round(td_net, 6), 'td_full_ann': round(td_net_ann, 6),
    'td_gross': round(td_gross, 6), 'td_gross_ann': round(td_gross_ann, 6),
    'turnover_avg': round(to_avg, 6),
    'annual': annual_new,
    # Paramètres de sélection documentés
    'selection_params': {
        # ── Composition du panier ─────────────────────────────────────────
        'methode':                   'Hybride OTC top-N + ADV-cap redistribution',
        'force_top_n':               FORCE_TOP_N,
        'force_top_n_note':          f'Top {FORCE_TOP_N} titres BRVM30 tenus à leur poids exact via OTC (no ADV constraint)',
        'max_exec_large_days':       MAX_EXEC_LARGE,
        'max_exec_small_days':       MAX_EXEC_SMALL,
        'large_threshold_pct':       LARGE_THRESHOLD * 100,
        'participation_rate_pct':    PARTICIPATION_RATE * 100,
        'min_adv_mfcfa':             MIN_ADV_MFCFA,
        'min_basket_weight_pct':     MIN_BASKET_WEIGHT * 100,
        # ── Rebalancement ────────────────────────────────────────────────
        'rebal_cible_freq':          'trimestriel (jan/avr/jul/oct)',
        'rebal_execution_freq':      'mensuel avec seuil de dérive',
        'drift_threshold_pct':       1.0,
        'drift_note':                'Trade uniquement si |w_actuel - w_cible| > 1% pour un titre',
        # ── Coûts de transaction ─────────────────────────────────────────
        'spread_model':              'variable selon ADV : 25 bps (>=100 MFCFA) → 175 bps (<5 MFCFA)',
        'spread_25bps_above_mfcfa':  100,
        'spread_40bps_above_mfcfa':  30,
        'spread_80bps_above_mfcfa':  10,
        'spread_125bps_above_mfcfa': 5,
        'spread_175bps_below_mfcfa': 5,
        # ── Dividendes ───────────────────────────────────────────────────
        'dividende_model':           'Total Return avec reserve capitalisée',
        'dividende_ex_date':         '1er juillet de l\'année N+1 pour exercice N',
        'dividende_placement_taux':  RF_RATE_ANN * 100,
        'dividende_distribution':    'dernier jour de bourse de juin et décembre',
        # ── Frais et AUM ─────────────────────────────────────────────────
        'mgmt_fee_ann_pct':          MGMT_FEE_ANN * 100,
        'aum_reference_mfcfa':       AUM_MFCFA,
    }
})
json.dump(bm, open(BM_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════════════════════════════
# TESTS DE VALIDATION (avec sélection de panier réelle pour les stress tests)
# ══════════════════════════════════════════════════════════════════════════════
print("[6/6] Génération des tests de validation…")


def stress_with_selection(name, rebal_freq_months, fee=MGMT_FEE_ANN,
                          aum=AUM_MFCFA,
                          max_small=MAX_EXEC_SMALL, max_large=MAX_EXEC_LARGE,
                          monthly_rebal=True, drift_threshold=0.01):
    """Stress test avec fréquence de mise à jour de la cible + seuil de dérive."""
    rb_new = [START_DATE]
    last_ts = pd.Timestamp(START_DATE)
    for dt in all_dates[1:]:
        ts = pd.Timestamp(dt)
        if (ts.year - last_ts.year) * 12 + (ts.month - last_ts.month) >= rebal_freq_months:
            rb_new.append(dt)
            last_ts = ts

    wh_new = {}
    for rb in rb_new:
        past_dates = [d for d in brvm30_weights_hist if d <= rb]
        closest = max(past_dates) if past_dates else min(brvm30_weights_hist.keys())
        w_b30 = brvm30_weights_hist.get(closest, {})
        past_rd = [d for d in rebal_dates if d <= rb]
        closest_rd = max(past_rd) if past_rd else rebal_dates[0]
        bw = build_basket(rb, w_b30, aum, max_small, max_large)
        wh_new[rb] = bw if bw else _get_weights(w_history, closest_rd)

    ng, nn, _ = build_nav_tr(all_dates, sh, rb_new, wh_new, fee,
                             monthly_rebal=monthly_rebal,
                             drift_threshold=drift_threshold)
    te_s, td_s, _ = compute_te_td(nn, bench_s)

    tos = []
    for i in range(1, len(rb_new)):
        wp = wh_new[rb_new[i-1]]; wc = wh_new[rb_new[i]]
        tks = set(wp) | set(wc)
        tos.append(sum(abs(wc.get(t,0)-wp.get(t,0)) for t in tks)/2)
    to_s = float(np.mean(tos)) if tos else 0.0

    return {'name': name, 'te': round(te_s, 6), 'td': round(td_s, 6),
            'turnover': round(to_s, 6)}


stress_tests = [
    stress_with_selection('Mensuel dérive 1% (référence)',  3, monthly_rebal=True,  drift_threshold=0.01),
    stress_with_selection('Trimestriel classique',          3, monthly_rebal=False, drift_threshold=0.0),
    stress_with_selection('Mensuel dérive 0% (trade tout)', 3, monthly_rebal=True,  drift_threshold=0.0),
    stress_with_selection('Mensuel dérive 2%',              3, monthly_rebal=True,  drift_threshold=0.02),
    stress_with_selection('Trimestriel +frais ×2',          3, monthly_rebal=False, fee=MGMT_FEE_ANN*2),
    stress_with_selection('Trimestriel 0-frais',            3, monthly_rebal=False, fee=0.0),
]
print("   Stress tests OK:", [s['name'] for s in stress_tests])

# ── Sensibilité seuil grand titre (LARGE_THRESHOLD) ──────────────────────────
# Teste différentes valeurs de LARGE_THRESHOLD : au-dessus du seuil = 62j max, en dessous = 32j
ewma_sensitivity = []
for threshold in [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15]:
    wh_sim = {}
    for rb in rebal_dates:
        _past = [d for d in brvm30_weights_hist if d <= rb]
        closest = max(_past) if _past else min(brvm30_weights_hist.keys())
        w_b30 = brvm30_weights_hist.get(closest, {})
        wh_sim[rb] = build_basket(rb, w_b30, AUM_MFCFA,
                                  max_small=MAX_EXEC_SMALL, max_large=MAX_EXEC_LARGE,
                                  large_thr=threshold)

    ng, nn, _ = build_nav_tr(all_dates, sh, rebal_dates, wh_sim,
                             monthly_rebal=True, drift_threshold=0.01)
    te_f, td_f, _ = compute_te_td(nn, bench_s)
    to_f = turnover_avg(wh_sim, rebal_dates)
    n_large_avg = int(np.mean([sum(1 for tk in wh_sim[d]
                                   if brvm30_weights_hist.get(
                                       max((x for x in brvm30_weights_hist if x <= d),
                                           default=min(brvm30_weights_hist.keys())),{}
                                   ).get(tk, 0) >= threshold)
                               for d in rebal_dates]))
    ewma_sensitivity.append({'threshold': threshold, 'te': round(te_f, 6),
                              'td': round(td_f, 6), 'turnover': round(to_f, 6),
                              'n_large_avg': n_large_avg})
    print("   Seuil grand ≥%.0f%% (62j): TE=%.2f%%  TD=%+.2f%%  n_grands≈%d" % (
        threshold*100, te_f*100, td_f*100, n_large_avg))

# ── Bootstrap TE ─────────────────────────────────────────────────────────────
r_e = nav_net_pr.pct_change().dropna()
r_b = bench_s.pct_change().dropna()
ci  = r_e.index.intersection(r_b.index)
diff_daily = (r_e[ci] - r_b[ci]).values
N_SIM = 500
te_boots = [float(np.random.choice(diff_daily, len(diff_daily), replace=True).std(ddof=1) * np.sqrt(252))
            for _ in range(N_SIM)]
boot = {'n_sim': N_SIM,
        'te_med': round(float(np.median(te_boots)), 6),
        'te_p5':  round(float(np.percentile(te_boots, 5)),  6),
        'te_p95': round(float(np.percentile(te_boots, 95)), 6)}
print("   Bootstrap: TE médiane=%.2f%%  [%.2f%% – %.2f%%]" % (
    boot['te_med']*100, boot['te_p5']*100, boot['te_p95']*100))

# ── Walk-Forward ─────────────────────────────────────────────────────────────
wf_results = []
splits = [
    ('Jan–Déc 2023',         '2023-01-02','2023-12-29','2024-01-02','2024-12-31'),
    ('Jan–Déc 2024',         '2024-01-02','2024-12-31','2025-01-02','2025-12-31'),
    ('Jan–Déc 2025',         '2025-01-02','2025-12-31','2026-01-02','2026-04-01'),
    ('2023–2024 IS/2025 OOS','2023-01-02','2024-12-31','2025-01-02','2025-12-31'),
    ('2023–2025 IS/2026 OOS','2023-01-02','2025-12-31','2026-01-02','2026-04-01'),
]
for label, is_s, is_e, oos_s, oos_e in splits:
    e_is  = nav_net_pr[[d for d in nav_net_pr.index if is_s  <= d <= is_e]]
    b_is  = bench_s[[d for d in bench_s.index       if is_s  <= d <= is_e]]
    e_oos = nav_net_pr[[d for d in nav_net_pr.index if oos_s <= d <= oos_e]]
    b_oos = bench_s[[d for d in bench_s.index       if oos_s <= d <= oos_e]]
    if len(e_oos) < 5:
        continue
    te_is,  _, _ = compute_te_td(e_is,  b_is)
    te_oos, td_oos, _ = compute_te_td(e_oos, b_oos)
    wf_results.append({'label': label, 'te_is': round(te_is, 6),
                       'te_oos': round(te_oos, 6), 'td_oos': round(td_oos, 6),
                       'delta_te': round(te_oos - te_is, 6)})
    print("   WF %s: IS=%.2f%% OOS=%.2f%%" % (label, te_is*100, te_oos*100))

vl = {'stress_tests': stress_tests, 'ewma_sensitivity': ewma_sensitivity,
      'bootstrap': boot, 'walk_forward': wf_results,
      'selection_params': bm['selection_params']}
json.dump(vl, open(VL_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

# ── Scalabilité — backtest complet par palier d'AUM ─────────────────────────
# Paliers : 5, 10, 15, 20, 25, 30, 40, 50 Md FCFA
AUM_PALIERS = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000, 40_000, 50_000]

sc_results = []

for aum in AUM_PALIERS:
    sc_label = f'{aum // 1000} Md FCFA' + (' (actuel)' if aum == AUM_MFCFA else '')
    print(f"   Scalabilité {sc_label}…", flush=True)

    # Recalcul du panier à chaque rebalancement avec cet AUM
    wh_sc = {}
    rebal_detail_sc = []   # détail par trimestre

    for rb in rebal_dates:
        past = [d for d in brvm30_weights_hist if d <= rb]
        closest = max(past) if past else min(brvm30_weights_hist.keys())
        w_b30 = brvm30_weights_hist.get(closest, {})

        # build_basket avec ADV-cap 62j/32j au nouvel AUM
        bsk = build_basket(rb, w_b30, aum, MAX_EXEC_SMALL, MAX_EXEC_LARGE)
        if not bsk:
            past_rd = [d for d in rebal_dates if d <= rb]
            closest_rd = max(past_rd) if past_rd else rebal_dates[0]
            bsk = _get_weights(w_history, closest_rd)
        wh_sc[rb] = bsk

        # Titres plafonnés = ceux dont le poids ETF < poids BRVM30 normalisé aux éligibles
        eligible_b30 = {tk: w for tk, w in w_b30.items() if tk in bsk}
        tot_elig = sum(eligible_b30.values()) or 1.0
        w_b30_norm = {tk: v / tot_elig for tk, v in eligible_b30.items()}
        capped = [tk for tk in bsk if bsk[tk] < w_b30_norm.get(tk, 0) - 5e-5]
        excluded = [tk for tk in w_b30 if tk not in bsk]

        # ADV à cette date pour les titres plafonnés
        adv_rb = {tk: compute_adv(tk, rb) for tk in (capped + excluded)}

        rebal_detail_sc.append({
            'date':      rb,
            'basket_n':  len(bsk),
            'exclu_n':   len(excluded),
            'capped_n':  len(capped),
            'exclu':     sorted(excluded),
            'capped':    sorted(capped),
            'coverage':  round(sum(w_b30.get(tk, 0) for tk in bsk), 4),
            'adv_capped': {tk: round(adv_rb.get(tk, 0), 1) for tk in capped},
            'adv_exclu':  {tk: round(adv_rb.get(tk, 0), 1) for tk in excluded},
        })

    # NAV complète sur tout l'historique avec ce panier (mensuel + dérive 1%)
    ng_sc, nn_sc, _ = build_nav_tr(all_dates, sh, rebal_dates, wh_sc,
                                   monthly_rebal=True, drift_threshold=0.01)
    te_sc, td_sc, _ = compute_te_td(nn_sc, bench_s)

    # Turnover moyen
    tos_sc = []
    for i in range(1, len(rebal_dates)):
        wp = wh_sc[rebal_dates[i-1]]; wc = wh_sc[rebal_dates[i]]
        tks = set(wp) | set(wc)
        tos_sc.append(sum(abs(wc.get(t, 0) - wp.get(t, 0)) for t in tks) / 2)
    to_sc = float(np.mean(tos_sc)) if tos_sc else 0.0

    # Coût de transaction annualisé
    cost_tx_ann = 0.005 * to_sc * 4

    # Stats agrégées sur les trimestres
    n_capped_avg  = float(np.mean([r['capped_n']  for r in rebal_detail_sc]))
    n_exclu_avg   = float(np.mean([r['exclu_n']   for r in rebal_detail_sc]))
    coverage_avg  = float(np.mean([r['coverage']  for r in rebal_detail_sc]))
    basket_n_avg  = float(np.mean([r['basket_n']  for r in rebal_detail_sc]))

    # Titres le plus souvent plafonnés ou exclus sur tout l'historique
    from collections import Counter
    capped_counter  = Counter(tk for r in rebal_detail_sc for tk in r['capped'])
    exclu_counter   = Counter(tk for r in rebal_detail_sc for tk in r['exclu'])
    n_rebals        = len(rebal_detail_sc)
    top_capped  = [{'ticker': tk, 'freq': round(cnt/n_rebals, 2)}
                   for tk, cnt in capped_counter.most_common(10)]
    top_exclu   = [{'ticker': tk, 'freq': round(cnt/n_rebals, 2)}
                   for tk, cnt in exclu_counter.most_common(10)]

    sc_results.append({
        'aum_mfcfa':      aum,
        'label':          sc_label,
        'te':             round(te_sc, 6),
        'td':             round(td_sc, 6),
        'turnover':       round(to_sc, 6),
        'cost_tx_ann':    round(cost_tx_ann, 6),
        'basket_n_avg':   round(basket_n_avg, 1),
        'n_capped_avg':   round(n_capped_avg, 1),
        'n_exclu_avg':    round(n_exclu_avg, 1),
        'coverage_avg':   round(coverage_avg, 4),
        'top_capped':     top_capped,
        'top_exclu':      top_exclu,
        'rebal_detail':   rebal_detail_sc,
    })
    print("     TE=%.2f%%  TD=%+.2f%%  plafonnés≈%.1f  exclus≈%.1f  couverture=%.1f%%" % (
        te_sc*100, td_sc*100, n_capped_avg, n_exclu_avg, coverage_avg*100))

json.dump(sc_results, open(SC_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))

print()
print("=== TERMINÉ ===")
print("NAV net PR : %.2f → %.2f  (TD = %+.2f%%)" % (
    nav_net_pr.iloc[0], nav_net_pr.iloc[-1], td_net*100))
print("BRVM30 PR  : %.2f → %.2f" % (bench_s.iloc[0], bench_s.iloc[-1]))
print("TE nette : %.2f%%   TE brute : %.2f%%" % (te_net*100, te_gross*100))
print()
print("Paramètres de sélection documentés :")
for k, v in bm['selection_params'].items():
    print("  %s: %s" % (k, v))
