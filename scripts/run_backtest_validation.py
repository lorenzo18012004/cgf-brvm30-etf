"""
Reconstruction complète du backtest en Price Return pur (dividendes exclus)
et génération des tests de validation : stress, walk-forward, scalabilité, bootstrap.

Règles de sélection du panier (appliquées dans les scénarios stress) :
  1. Force si w_brvm30 >= FORCE_WEIGHT  (3%) — inclus malgré ADV ou stale insuffisant
  2. Exclusion stale si >= STALE_THRESH (70%) sur STALE_WINDOW jours glissants
       → sauf si poids >= FORCE_WEIGHT
  3. Exclusion ADV : si exécution > MAX_EXEC_NEW_DAYS  (100j) pour nouveaux entrants
                     si exécution > MAX_EXEC_EXIST_DAYS (32j)  pour titres déjà en portefeuille
                     → exit déclenché seulement après 2 rebalancements consécutifs > seuil
  4. Exclusion Float < FLOAT_MIN_MFCFA (7 000 M FCFA)
  5. Exclusion si poids redistribué < MIN_BASKET_WEIGHT (0.1%)

Usage :  python scripts/run_backtest_validation.py
Sorties :
  data/dashboard_data.json      → nav_etf, nav_gross, nav_bench mis à jour
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
FORCE_WEIGHT      = 0.03    # Force inclusion si poids BRVM30 >= 3%
STALE_THRESH      = 0.70    # Exclusion si >= 70% de jours stale
STALE_WINDOW      = 63      # Fenêtre de calcul stale : 3 mois (≈ 63 jours ouvrés)
MAX_EXEC_NEW_DAYS = 100     # Nouveaux entrants : max 100j d'exécution (bloc OTC)
MAX_EXEC_EXIST_DAYS = 32    # Titres existants : max 32j d'exécution
CONSEC_REBALS_EXIT  = 2     # Nb de rebalancements consécutifs > seuil avant sortie
FLOAT_MIN_MFCFA   = 7_000  # Float minimum en M FCFA
MIN_BASKET_WEIGHT = 0.001   # Poids minimum dans le panier après redistribution (0.1%)
MGMT_FEE_ANN      = 0.006  # Frais de gestion : 0.60%/an
AUM_MFCFA         = 5_000  # AUM de référence en M FCFA (5 Md)

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

# ── BRVM30 PR depuis Excel ────────────────────────────────────────────────────
print("[2/6] Lecture BRVM30 PR depuis Excel…")
import openpyxl
wb   = openpyxl.load_workbook(EX_PATH, read_only=True, data_only=True)
ws   = wb['🏛️ BRVM_Indices']
rows = list(ws.iter_rows(values_only=True))
wb.close()
brvm30_raw = {}
for r in rows[1:]:
    if r[0] is None or r[2] is None:
        continue
    brvm30_raw[pd.Timestamp(r[0]).strftime('%Y-%m-%d')] = float(r[2])

# Toutes les dates de trading dans sika
all_dates = sorted({d for tk in sh for d in sh[tk]
                    if START_DATE <= d <= END_DATE})

# ══════════════════════════════════════════════════════════════════════════════
# MODULE DE SÉLECTION DU PANIER
# ══════════════════════════════════════════════════════════════════════════════

def compute_adv(ticker: str, as_of_date: str, window_days: int = 63) -> float:
    """ADV en M FCFA sur les `window_days` jours ouvrés précédant as_of_date.
    Le dénominateur inclut tous les jours de la fenêtre (y compris jours sans volume)
    pour ne pas surestimer la liquidité des titres semi-actifs."""
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window_days:]
    vals  = [(hist[d].get('volume', 0) or 0) * (hist[d].get('close', 0) or 0) / 1e6
             for d in dates]
    return float(sum(vals) / len(dates)) if dates else 0.0


def compute_stale_ratio(ticker: str, as_of_date: str,
                        window: int = STALE_WINDOW) -> float:
    """Fraction de jours sans transaction sur la fenêtre glissante."""
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    if not dates:
        return 1.0
    stale = sum(1 for d in dates
                if (hist[d].get('volume') or 0) == 0)
    return stale / len(dates)


def exec_days(ticker: str, trade_mfcfa: float, as_of_date: str) -> float:
    """Nombre de jours théoriques pour exécuter trade_mfcfa M FCFA sans impact."""
    adv = compute_adv(ticker, as_of_date)
    if adv <= 0:
        return 999.0
    return trade_mfcfa / adv


def select_basket(rebal_date: str,
                  brvm30_tickers: list,
                  w_brvm30: dict,
                  prev_basket: set,
                  excess_days_count: dict,   # {ticker: nb rebals consécutifs > seuil}
                  aum_mfcfa: float = AUM_MFCFA) -> tuple:
    """
    Applique les 5 règles de sélection et retourne (basket_weights, excluded, excess_days_count_new).

    basket_weights : {ticker: weight_etf}  — poids normalisés à 1
    excluded       : [{ticker, raison, ...}]
    """
    forced   = []   # toujours inclus
    included = []   # inclus (poids >= 3% ou ADV OK)
    excluded = []   # exclus avec raison

    excess_new = dict(excess_days_count)

    for tk in brvm30_tickers:
        w_b30 = w_brvm30.get(tk, 0.0)
        trade_mfcfa = w_b30 * aum_mfcfa   # taille du trade estimée

        # ── Règle 4 : Float ───────────────────────────────────────────────
        # (on ne dispose pas du float en temps réel → skip dans backtest,
        #  géré manuellement via la composition BRVM30 officielle)

        # ── Règle 2 : Prix stale ──────────────────────────────────────────
        stale = compute_stale_ratio(tk, rebal_date)
        if stale >= STALE_THRESH and w_b30 < FORCE_WEIGHT:
            excluded.append({'ticker': tk, 'raison': 'Stale %.0f%%' % (stale*100),
                             'w_brvm30': w_b30, 'stale_ratio': round(stale, 3)})
            excess_new.pop(tk, None)
            continue

        # ── Règle 1 : Force si poids >= 3% ───────────────────────────────
        if w_b30 >= FORCE_WEIGHT:
            forced.append((tk, w_b30))
            excess_new.pop(tk, None)
            continue

        # ── Règle 3 : ADV / jours d'exécution ────────────────────────────
        adv     = compute_adv(tk, rebal_date)
        is_new  = tk not in prev_basket
        ex_days = exec_days(tk, trade_mfcfa, rebal_date)
        max_days = MAX_EXEC_NEW_DAYS if is_new else MAX_EXEC_EXIST_DAYS

        if ex_days > max_days:
            if is_new:
                # Nouveau entrant : exclure directement si > 100j
                excluded.append({'ticker': tk, 'raison': 'ADV insuffisant (%.0fj > %dj)' % (ex_days, max_days),
                                 'w_brvm30': w_b30, 'adv_mfcfa': round(adv, 1), 'exec_days': round(ex_days, 1)})
                excess_new.pop(tk, None)
            else:
                # Titre existant : compter les rebals consécutifs au-dessus du seuil
                excess_new[tk] = excess_new.get(tk, 0) + 1
                if excess_new[tk] >= CONSEC_REBALS_EXIT:
                    excluded.append({'ticker': tk, 'raison': 'ADV insuffisant %d rebals consécutifs' % CONSEC_REBALS_EXIT,
                                     'w_brvm30': w_b30, 'adv_mfcfa': round(adv, 1), 'exec_days': round(ex_days, 1)})
                    excess_new.pop(tk, None)
                else:
                    # Tolérance : on garde encore ce rebal
                    included.append((tk, w_b30))
        else:
            # ADV OK
            excess_new.pop(tk, None)
            included.append((tk, w_b30))

    # Assembler basket : forcés + inclus
    basket_raw = forced + included
    if not basket_raw:
        return {}, excluded, excess_new

    # ── Règle 5 : Poids minimum 0.1% ─────────────────────────────────────
    total_w = sum(w for _, w in basket_raw)
    final   = []
    excl_min= []
    for tk, w_b30 in basket_raw:
        w_norm = w_b30 / total_w if total_w > 0 else 1 / len(basket_raw)
        if w_norm < MIN_BASKET_WEIGHT and w_b30 < FORCE_WEIGHT:
            excl_min.append({'ticker': tk, 'raison': 'Poids < %.1f%%' % (MIN_BASKET_WEIGHT*100),
                             'w_brvm30': w_b30, 'w_basket': round(w_norm, 4)})
        else:
            final.append((tk, w_b30))
    excluded += excl_min

    # Normaliser les poids finaux
    total_f = sum(w for _, w in final)
    basket_weights = {tk: round(w / total_f, 6) for tk, w in final} if total_f > 0 else {}

    return basket_weights, excluded, excess_new


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


COST_TX = 0.005   # 50 bps spread one-way

def build_nav_pr(all_dates, sh, rb_dates, wh, fee_ann=MGMT_FEE_ANN, cost_tx=COST_TX):
    """
    Retourne (nav_gross_pr, nav_net_pr) en Series indexées par date string.

    Corrections appliquées :
      1. Poids mark-to-market : les valeurs de positions dérivent avec les prix
         entre deux rebalancements (nombre d'actions fixe, pas les poids).
      2. Coût de transaction : 50bps × turnover one-way déduit à chaque rebal.
      3. Frais de gestion (nav_net) : formule récursive quotidienne (1-f)^(1/252)
         appliquée jour après jour — pas de recalcul depuis J0.
    """
    gross_pts, net_pts = {}, {}
    nav_gross  = 100.0
    nav_net    = 100.0
    _daily_fee = (1 - fee_ann) ** (1 / 252)

    rb_idx       = 0
    prev_weights = dict(_get_weights(wh, rb_dates[0]))

    # Portefeuille mark-to-market : {ticker: valeur_fraction}
    # Σ portfolio.values() = 1.0 à chaque rebalancement
    portfolio = dict(prev_weights)

    for i, dt in enumerate(all_dates):
        if i == 0:
            gross_pts[dt] = nav_gross
            net_pts[dt]   = nav_net
            continue

        prev_dt = all_dates[i - 1]

        # ── Rendement journalier avec poids PRÉ-rebalancement ─────────────────
        # Le rebalancement s'exécute à la clôture : le rendement du jour J
        # est calculé avec les poids AVANT rebalancement, les nouveaux poids
        # n'entrent en vigueur qu'à partir du jour J+1.
        total_prev    = sum(portfolio.values()) or 1.0
        new_portfolio = {}
        for tk, v in portfolio.items():
            p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
            p2 = sh.get(tk, {}).get(dt,      {}).get('close')
            new_portfolio[tk] = v * (p2 / p1) if (p1 and p2 and p1 > 0) else v

        total_new = sum(new_portfolio.values())
        r_t       = (total_new / total_prev - 1) if total_prev > 0 else 0.0
        portfolio = new_portfolio

        # ── Mise à jour NAV ───────────────────────────────────────────────────
        nav_gross *= (1 + r_t)
        nav_net   *= (1 + r_t) * _daily_fee

        # ── Rebalancement en clôture → coûts + reset pour le lendemain ───────
        while rb_idx + 1 < len(rb_dates) and dt >= rb_dates[rb_idx + 1]:
            rb_idx      += 1
            new_w        = _get_weights(wh, rb_dates[rb_idx])
            all_tks      = set(prev_weights) | set(new_w)
            total_port   = sum(portfolio.values()) or 1.0
            curr_w_norm  = {tk: v / total_port for tk, v in portfolio.items()}
            turnover     = sum(abs(new_w.get(t, 0) - curr_w_norm.get(t, 0))
                               for t in all_tks) / 2
            nav_gross   *= (1 - cost_tx * turnover)
            nav_net     *= (1 - cost_tx * turnover)
            portfolio    = dict(new_w)
            prev_weights = dict(new_w)

        gross_pts[dt] = nav_gross
        net_pts[dt]   = nav_net

    return pd.Series(gross_pts, dtype=float), pd.Series(net_pts, dtype=float)


nav_gross_pr, nav_net_pr = build_nav_pr(all_dates, sh, rebal_dates, w_history)
print("   NAV gross PR: %.2f → %.2f" % (nav_gross_pr.iloc[0], nav_gross_pr.iloc[-1]))
print("   NAV net   PR: %.2f → %.2f" % (nav_net_pr.iloc[0],   nav_net_pr.iloc[-1]))

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
                      fee_ann=MGMT_FEE_ANN, cost_tx=COST_TX):
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
        'force_weight_pct':       FORCE_WEIGHT * 100,
        'stale_threshold_pct':    STALE_THRESH * 100,
        'stale_window_days':      STALE_WINDOW,
        'max_exec_new_days':      MAX_EXEC_NEW_DAYS,
        'max_exec_exist_days':    MAX_EXEC_EXIST_DAYS,
        'consec_rebals_exit':     CONSEC_REBALS_EXIT,
        'float_min_mfcfa':        FLOAT_MIN_MFCFA,
        'min_basket_weight_pct':  MIN_BASKET_WEIGHT * 100,
        'mgmt_fee_ann_pct':       MGMT_FEE_ANN * 100,
    }
})
json.dump(bm, open(BM_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

# ══════════════════════════════════════════════════════════════════════════════
# TESTS DE VALIDATION (avec sélection de panier réelle pour les stress tests)
# ══════════════════════════════════════════════════════════════════════════════
print("[6/6] Génération des tests de validation…")


def stress_with_selection(name, rebal_freq_months, fee=MGMT_FEE_ANN,
                          aum=AUM_MFCFA):
    """Stress test avec fréquence de rebalancement différente + règles de sélection."""
    # Générer les dates de rebalancement
    rb_new = [START_DATE]
    last_ts = pd.Timestamp(START_DATE)
    for dt in all_dates[1:]:
        ts = pd.Timestamp(dt)
        if (ts.year - last_ts.year) * 12 + (ts.month - last_ts.month) >= rebal_freq_months:
            rb_new.append(dt)
            last_ts = ts

    # Construire les poids à chaque rebalancement avec les règles de sélection
    wh_new       = {}
    prev_basket  = set()
    excess_cnt   = {}   # {ticker: nb rebals consécutifs > seuil}

    for rb in rb_new:
        # Poids BRVM30 : dernière composition connue AVANT ou égale à rb (pas de look-ahead)
        past_dates = [d for d in brvm30_weights_hist if d <= rb]
        closest = max(past_dates) if past_dates else min(brvm30_weights_hist.keys())
        w_b30   = brvm30_weights_hist.get(closest, {})
        past_rd = [d for d in rebal_dates if d <= rb]
        closest_rd = max(past_rd) if past_rd else rebal_dates[0]
        tickers = list(w_b30.keys()) or list(_get_weights(w_history, closest_rd).keys())

        bw, _, excess_cnt = select_basket(rb, tickers, w_b30, prev_basket, excess_cnt, aum)
        wh_new[rb]  = bw if bw else _get_weights(w_history, closest_rd)
        prev_basket = set(wh_new[rb].keys())

    ng, nn = build_nav_pr(all_dates, sh, rb_new, wh_new, fee)
    te_s, td_s, _ = compute_te_td(nn, bench_s)

    # Turnover moyen
    tos = []
    for i in range(1, len(rb_new)):
        wp = wh_new[rb_new[i-1]]; wc = wh_new[rb_new[i]]
        tks = set(wp) | set(wc)
        tos.append(sum(abs(wc.get(t,0)-wp.get(t,0)) for t in tks)/2)
    to_s = float(np.mean(tos)) if tos else 0.0

    return {'name': name, 'te': round(te_s, 6), 'td': round(td_s, 6),
            'turnover': round(to_s, 6)}


stress_tests = [
    stress_with_selection('Trimestriel (référence)', 3),
    stress_with_selection('Mensuel',                 1),
    stress_with_selection('Semestriel',              6),
    stress_with_selection('Annuel',                 12),
    stress_with_selection('Trimestriel +frais ×2',   3, fee=MGMT_FEE_ANN*2),
    stress_with_selection('Trimestriel 0-frais',      3, fee=0.0),
]
print("   Stress tests OK:", [s['name'] for s in stress_tests])

# ── Sensibilité seuil force (au lieu d'EWMA) ─────────────────────────────────
ewma_sensitivity = []
for threshold in [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15]:
    # Simuler avec un seuil de force différent
    rb_sim    = rebal_dates
    prev_b    = set()
    exc_c     = {}
    wh_sim    = {}
    for rb in rb_sim:
        _past = [d for d in brvm30_weights_hist if d <= rb]
        closest = max(_past) if _past else min(brvm30_weights_hist.keys())
        w_b30   = brvm30_weights_hist.get(closest, {})
        tickers = list(w_b30.keys())

        # Sélection avec seuil de force modifié
        forced_tmp, included_tmp, excluded_tmp = [], [], []
        exc_new = dict(exc_c)
        for tk in tickers:
            w = w_b30.get(tk, 0.0)
            stale = compute_stale_ratio(tk, rb)
            if w >= threshold:
                forced_tmp.append((tk, w))
                exc_new.pop(tk, None)
            elif stale >= STALE_THRESH:
                excluded_tmp.append(tk)
                exc_new.pop(tk, None)
            else:
                adv    = compute_adv(tk, rb)
                trade  = w * AUM_MFCFA
                ex_d   = trade / adv if adv > 0 else 999
                is_new = tk not in prev_b
                mx     = MAX_EXEC_NEW_DAYS if is_new else MAX_EXEC_EXIST_DAYS
                if ex_d > mx:
                    if is_new:
                        excluded_tmp.append(tk)
                    else:
                        exc_new[tk] = exc_new.get(tk, 0) + 1
                        if exc_new[tk] >= CONSEC_REBALS_EXIT:
                            excluded_tmp.append(tk)
                        else:
                            included_tmp.append((tk, w))
                else:
                    exc_new.pop(tk, None)
                    included_tmp.append((tk, w))
        exc_c = exc_new

        basket_raw = forced_tmp + included_tmp
        total_w    = sum(w for _, w in basket_raw)
        wh_sim[rb] = {tk: round(w/total_w, 6) for tk, w in basket_raw} if total_w > 0 else {}
        prev_b     = set(wh_sim[rb].keys())

    ng, nn = build_nav_pr(all_dates, sh, rb_sim, wh_sim)
    te_f, td_f, _ = compute_te_td(nn, bench_s)
    to_f = turnover_avg(wh_sim, rb_sim)
    n_forced_avg = int(np.mean([sum(1 for tk in wh_sim[d]
                                    if brvm30_weights_hist.get(
                                        min(brvm30_weights_hist.keys(),
                                            key=lambda x: abs(pd.Timestamp(x)-pd.Timestamp(d))),{}
                                    ).get(tk, 0) >= threshold)
                                for d in rb_sim]))
    ewma_sensitivity.append({'threshold': threshold, 'te': round(te_f, 6),
                              'td': round(td_f, 6), 'turnover': round(to_f, 6),
                              'n_forced_avg': n_forced_avg})
    print("   Force ≥%.0f%%: TE=%.2f%%  TD=%+.2f%%  n_forced≈%d" % (
        threshold*100, te_f*100, td_f*100, n_forced_avg))

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

# ── Scalabilité ──────────────────────────────────────────────────────────────
sc_results = []
aum_scenarios = [('500M FCFA (actuel)', 500), ('1 Md FCFA', 1_000),
                 ('2.5 Md FCFA', 2_500), ('5 Md FCFA', 5_000), ('10 Md FCFA', 10_000)]

for sc_name, aum in aum_scenarios:
    result = stress_with_selection('Trimestriel (référence)', 3, aum=aum)
    cost_tx_ann  = 0.005 * result['turnover'] * 4   # spread 50bps × turnover scénario × 4 rebals
    cost_tx_cumul = cost_tx_ann * (len(all_dates) / 252)
    sc_results.append({'scenario': sc_name, 'aum_fcfa': aum * 1e6,
                       'te': result['te'], 'td': result['td'],
                       'turnover': result['turnover'],
                       'cost_tx_cumul': round(cost_tx_cumul, 6),
                       'basket_n_avg': bm.get('n_titres_avg', 27)})
    print("   Scalabilité %s: TE=%.2f%%  TD=%+.2f%%" % (sc_name, result['te']*100, result['td']*100))

json.dump(sc_results, open(SC_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

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
