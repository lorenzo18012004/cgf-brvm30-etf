"""
Calcul TE progressive vs TE instantanée.
Simule l'exécution graduelle des rebalancements sur j_exec jours (basé sur ADV delta).
"""
import json, os, sys
import numpy as np
import pandas as pd
import openpyxl
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')

dd = json.load(open(os.path.join(DATA, 'dashboard_data.json'), encoding='utf-8'))
sh = json.load(open(os.path.join(DATA, 'sika_history.json'), encoding='utf-8'))
rd = json.load(open(os.path.join(DATA, 'rebal_detail.json'), encoding='utf-8'))
bm = json.load(open(os.path.join(DATA, 'backtest_metrics.json'), encoding='utf-8'))

AUM_MFCFA    = 5_000
MGMT_FEE_ANN = 0.006
COST_TX      = 0.005
START_DATE   = '2023-01-02'
END_DATE     = '2026-04-01'

w_history   = dd['w_history']
rebal_dates = sorted(w_history.keys())
all_dates   = sorted({d for tk in sh for d in sh[tk] if START_DATE <= d <= END_DATE})

def _get_w(wh, dt):
    val = wh[dt]
    if isinstance(val, dict): return val
    if isinstance(val, list) and len(val) == 2: return val[1]
    return {}

# ── Benchmark depuis Sika (brvm30_index_history.json) ────────────────────────
_brvm30_raw = json.load(open(os.path.join(DATA, 'brvm30_index_history.json'), encoding='utf-8'))
brvm30_raw = {k: float(v) for k, v in _brvm30_raw.items() if v}
base_val = brvm30_raw[START_DATE]
last_pr = 100.0
bench_pts = {}
for dt in all_dates:
    if dt in brvm30_raw:
        last_pr = brvm30_raw[dt] / base_val * 100
    bench_pts[dt] = last_pr
bench_s = pd.Series(bench_pts, dtype=float)

# ── ADV map depuis rebal_detail ───────────────────────────────────────────────
adv_map = {}
for r in rd.get('rebalancings', []):
    dt = r.get('date')
    m = {}
    for item in r.get('basket', []) + r.get('excluded', []):
        tk = item.get('ticker', '')
        adv = item.get('adv_mfcfa', 0)
        if tk and adv:
            m[tk] = adv
    if m:
        adv_map[dt] = m

# ── Exec days (delta-based) ───────────────────────────────────────────────────
exec_days_map = {}
for i, dt in enumerate(rebal_dates):
    new_w = _get_w(w_history, dt)
    prev_w = _get_w(w_history, rebal_dates[i-1]) if i > 0 else {}
    adv_dt = adv_map.get(dt, {})
    n_max = 1.0
    worst = ('—', 0.0)
    for tk in set(prev_w) | set(new_w):
        delta = abs(new_w.get(tk, 0) - prev_w.get(tk, 0))
        adv = adv_dt.get(tk, 0)
        if adv > 0 and delta > 0:
            j = (delta * AUM_MFCFA) / adv
            if j > n_max:
                n_max = j
                worst = (tk, j)
    n_exec = int(np.ceil(min(n_max, 30)))
    exec_days_map[dt] = n_exec
    print(f"  {dt}: {n_exec} jours (max: {worst[0]} {worst[1]:.1f}j)")

print()

# ── Fonctions de calcul ───────────────────────────────────────────────────────
def compute_te_td(nav_s, bench_s_):
    idx = nav_s.index.intersection(bench_s_.index)
    e, b = nav_s[idx], bench_s_[idx]
    r_e = e.pct_change().dropna()
    r_b = b.pct_change().dropna()
    ci  = r_e.index.intersection(r_b.index)
    te  = float((r_e[ci] - r_b[ci]).std(ddof=1) * np.sqrt(252))
    td  = float(e.iloc[-1] / e.iloc[0] / (b.iloc[-1] / b.iloc[0]) - 1)
    n_cal = (pd.Timestamp(e.index[-1]) - pd.Timestamp(e.index[0])).days
    n_y   = n_cal / 365.25 if n_cal > 0 else len(e) / 252
    td_ann = float((1 + td) ** (1 / n_y) - 1) if n_y > 0 else 0.0
    return te, td, td_ann


def build_nav_instantaneous(all_dates, sh, rb_dates, wh):
    gross_pts, net_pts = {}, {}
    nav_gross = 100.0
    nav_net   = 100.0
    _fee = (1 - MGMT_FEE_ANN) ** (1 / 252)
    rb_idx = 0
    prev_weights = dict(_get_w(wh, rb_dates[0]))
    portfolio    = dict(prev_weights)

    for i, dt in enumerate(all_dates):
        if i == 0:
            gross_pts[dt] = nav_gross
            net_pts[dt]   = nav_net
            continue
        prev_dt = all_dates[i - 1]
        total_prev = sum(portfolio.values()) or 1.0
        new_portfolio = {}
        for tk, v in portfolio.items():
            p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
            p2 = sh.get(tk, {}).get(dt,      {}).get('close')
            new_portfolio[tk] = v * (p2 / p1) if (p1 and p2 and p1 > 0) else v
        r_t = sum(new_portfolio.values()) / total_prev - 1
        portfolio = new_portfolio
        nav_gross *= (1 + r_t)
        nav_net   *= (1 + r_t) * _fee
        while rb_idx + 1 < len(rb_dates) and dt >= rb_dates[rb_idx + 1]:
            rb_idx += 1
            new_w      = _get_w(wh, rb_dates[rb_idx])
            all_tks    = set(prev_weights) | set(new_w)
            total_port = sum(portfolio.values()) or 1.0
            curr_norm  = {tk: v / total_port for tk, v in portfolio.items()}
            to = sum(abs(new_w.get(t, 0) - curr_norm.get(t, 0)) for t in all_tks) / 2
            nav_gross  *= (1 - COST_TX * to)
            nav_net    *= (1 - COST_TX * to)
            portfolio   = dict(new_w)
            prev_weights = dict(new_w)
        gross_pts[dt] = nav_gross
        net_pts[dt]   = nav_net
    return pd.Series(gross_pts, dtype=float), pd.Series(net_pts, dtype=float)


def build_nav_progressive(all_dates, sh, rb_dates, wh, exec_days_map):
    """
    NAV avec exécution progressive des rebalancements.
    Transition linéaire des poids sur exec_days_map[rb_date] jours de bourse.
    Coûts de transaction répartis uniformément sur les jours d'exécution.
    Le portefeuille de référence est mis à jour mark-to-market à chaque jour,
    et réinitialisé aux poids cibles en fin de transition.
    """
    gross_pts, net_pts = {}, {}
    nav_gross = 100.0
    nav_net   = 100.0
    _fee = (1 - MGMT_FEE_ANN) ** (1 / 252)
    rb_idx    = 0
    portfolio = dict(_get_w(wh, rb_dates[0]))
    transition = None  # {old_w, new_w, n_days, day_k, daily_cost}

    for i, dt in enumerate(all_dates):
        if i == 0:
            gross_pts[dt] = nav_gross
            net_pts[dt]   = nav_net
            continue
        prev_dt = all_dates[i - 1]

        # ── Nouveau rebalancement ? ──────────────────────────────────────────
        while rb_idx + 1 < len(rb_dates) and dt >= rb_dates[rb_idx + 1]:
            rb_idx  += 1
            new_w    = _get_w(wh, rb_dates[rb_idx])
            n_exec   = exec_days_map.get(rb_dates[rb_idx], 1)
            total_port = sum(portfolio.values()) or 1.0
            old_w    = {tk: v / total_port for tk, v in portfolio.items()}
            all_tks_r = set(old_w) | set(new_w)
            to = sum(abs(new_w.get(t, 0) - old_w.get(t, 0)) for t in all_tks_r) / 2
            # Coût total réparti sur les n_exec jours d'exécution
            daily_cost = COST_TX * to / n_exec
            transition = {'old_w': dict(old_w), 'new_w': dict(new_w),
                          'n_days': n_exec, 'day_k': 0, 'daily_cost': daily_cost}

        # ── Poids effectifs (interpolation linéaire durant transition) ───────
        if transition is not None:
            frac = min(transition['day_k'] / transition['n_days'], 1.0)
            all_tks_t = set(transition['old_w']) | set(transition['new_w'])
            eff_w = {tk: transition['old_w'].get(tk, 0) * (1 - frac) +
                         transition['new_w'].get(tk, 0) * frac
                     for tk in all_tks_t}
            cost_today = transition['daily_cost']
            transition['day_k'] += 1
            if transition['day_k'] >= transition['n_days']:
                # Transition terminée → réinitialiser portfolio aux poids cibles
                portfolio  = dict(transition['new_w'])
                transition = None
        else:
            total_port = sum(portfolio.values()) or 1.0
            eff_w = {tk: v / total_port for tk, v in portfolio.items()}
            cost_today = 0.0

        # ── Rendement journalier (basé sur les poids effectifs) ──────────────
        total_eff = sum(eff_w.values()) or 1.0
        r_t = 0.0
        for tk, v in eff_w.items():
            p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
            p2 = sh.get(tk, {}).get(dt,      {}).get('close')
            if p1 and p2 and p1 > 0:
                r_t += (v / total_eff) * (p2 / p1 - 1)

        # ── Coût + mise à jour NAV ────────────────────────────────────────────
        nav_gross *= (1 + r_t) * (1 - cost_today)
        nav_net   *= (1 + r_t) * (1 - cost_today) * _fee

        # ── Mise à jour portefeuille mark-to-market (toujours, hors transition)
        # En transition : le portfolio est réinitialisé à la fin (new_w)
        # Hors transition : mark-to-market normal sur les poids détenus
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


# ── Calcul ────────────────────────────────────────────────────────────────────
print("Calcul NAV instantanée (référence)…")
nav_g_inst, nav_n_inst = build_nav_instantaneous(all_dates, sh, rebal_dates, w_history)
te_inst, td_inst, td_ann_inst = compute_te_td(nav_n_inst, bench_s)
print(f"  TE instantanée = {te_inst*100:.3f}%  |  TD cumulé = {td_inst*100:+.2f}%  |  TD/an = {td_ann_inst*100:+.2f}%")

print("Calcul NAV progressive…")
nav_g_prog, nav_n_prog = build_nav_progressive(all_dates, sh, rebal_dates, w_history, exec_days_map)
te_prog, td_prog, td_ann_prog = compute_te_td(nav_n_prog, bench_s)
print(f"  TE progressive  = {te_prog*100:.3f}%  |  TD cumulé = {td_prog*100:+.2f}%  |  TD/an = {td_ann_prog*100:+.2f}%")

print()
print(f"Impact execution progressive :")
print(f"  TE : {te_inst*100:.3f}% → {te_prog*100:.3f}%  (delta = +{(te_prog-te_inst)*100:.3f}pp)")
print(f"  TD : {td_inst*100:+.2f}% → {td_prog*100:+.2f}%  (delta = {(td_prog-td_inst)*100:+.2f}pp)")

# ── Sauvegarde dans backtest_metrics.json ─────────────────────────────────────
bm['te_prog'] = round(te_prog, 6)
bm['td_prog'] = round(td_prog, 6)
bm['exec_days_by_rebal'] = exec_days_map
BM_PATH = os.path.join(DATA, 'backtest_metrics.json')
json.dump(bm, open(BM_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print()
print(f"Sauvegardé → backtest_metrics.json : te_prog={te_prog:.6f}")
