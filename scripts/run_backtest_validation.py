"""
Reconstruction complète du backtest en Price Return pur (dividendes exclus)
et génération des tests de validation : stress, walk-forward, scalabilité, bootstrap.

Usage :  python scripts/run_backtest_validation.py
Sorties :
  data/dashboard_data.json  → nav_etf_pr, nav_gross_pr, nav_bench mis à jour
  data/validation_results.json
  data/scalability_results.json
  data/backtest_metrics.json
"""
import sys, os, json, math, random, numpy as np, pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

# ── Chemins ───────────────────────────────────────────────────────────────────
BASE       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA       = os.path.join(BASE, 'data')
EXCEL_DIR  = os.path.join(BASE, 'excel')

DD_PATH   = os.path.join(DATA, 'dashboard_data.json')
BM_PATH   = os.path.join(DATA, 'backtest_metrics.json')
VL_PATH   = os.path.join(DATA, 'validation_results.json')
SC_PATH   = os.path.join(DATA, 'scalability_results.json')
SH_PATH   = os.path.join(DATA, 'sika_history.json')
EX_PATH   = os.path.join(EXCEL_DIR, 'BRVM_Consolidated_Kendall_updated.xlsx')

MGMT_FEE_ANN = 0.006    # 0.60 %/an
START_DATE   = '2023-01-02'
END_DATE     = '2026-04-01'

random.seed(42)
np.random.seed(42)

# ── Chargement données ────────────────────────────────────────────────────────
print("[1/6] Chargement des données…")
dd = json.load(open(DD_PATH, encoding='utf-8'))
sh = json.load(open(SH_PATH, encoding='utf-8'))
bm = json.load(open(BM_PATH, encoding='utf-8'))

w_history = dd['w_history']          # {date: {ticker: weight}}
rebal_dates = sorted(w_history.keys())

# ── BRVM30 PR depuis Excel ────────────────────────────────────────────────────
print("[2/6] Lecture BRVM30 PR depuis Excel…")
import openpyxl
wb  = openpyxl.load_workbook(EX_PATH, read_only=True, data_only=True)
ws  = wb['🏛️ BRVM_Indices']
rows = list(ws.iter_rows(values_only=True))
wb.close()
brvm30_raw = {}
for r in rows[1:]:
    if r[0] is None or r[2] is None: continue
    brvm30_raw[pd.Timestamp(r[0]).strftime('%Y-%m-%d')] = float(r[2])

# ── Toutes les dates de trading disponibles dans sika ────────────────────────
all_dates = sorted({d for tk in sh for d in sh[tk]
                    if START_DATE <= d <= END_DATE})

# ── Reconstruction NAV Price Return ──────────────────────────────────────────
print("[3/6] Reconstruction NAV Price Return (sika prices, pas de dividendes)…")

def build_nav_pr(all_dates, sh, rebal_dates, w_history, fee_ann=MGMT_FEE_ANN):
    """Retourne (nav_gross_pr, nav_net_pr) en Series indexées par date string."""
    gross_pts = {}
    net_pts   = {}
    nav_gross = 100.0
    nav_net   = 100.0

    rb_idx = 0
    weights = _get_weights(w_history, rebal_dates[0])

    start_ts = pd.Timestamp(START_DATE)

    for i, dt in enumerate(all_dates):
        if i == 0:
            gross_pts[dt] = nav_gross
            net_pts[dt]   = nav_net
            continue

        prev_dt = all_dates[i - 1]

        # Passer au nouveau poids dès qu'on atteint la date de rebalancement
        while rb_idx + 1 < len(rebal_dates) and dt >= rebal_dates[rb_idx + 1]:
            rb_idx += 1
            weights = _get_weights(w_history, rebal_dates[rb_idx])

        # Return journalier du panier (prix purs, pas de dividendes)
        total_w, weighted_ret = 0.0, 0.0
        for tk, w in weights.items():
            p1 = sh.get(tk, {}).get(prev_dt, {}).get('close')
            p2 = sh.get(tk, {}).get(dt,      {}).get('close')
            if p1 and p2 and p1 > 0:
                weighted_ret += w * (p2 / p1 - 1)
                total_w      += w

        if total_w > 0:
            r = weighted_ret / total_w
        else:
            r = 0.0

        nav_gross *= (1 + r)

        # Frais de gestion journalisés
        days_since_start = (pd.Timestamp(dt) - start_ts).days
        fee_factor = (1 - fee_ann) ** (days_since_start / 365)
        nav_net = nav_gross * fee_factor

        gross_pts[dt] = nav_gross
        net_pts[dt]   = nav_net

    return pd.Series(gross_pts, dtype=float), pd.Series(net_pts, dtype=float)


def _get_weights(w_history, date_key):
    val = w_history[date_key]
    if isinstance(val, dict):
        return val
    if isinstance(val, list) and len(val) == 2 and isinstance(val[1], dict):
        return val[1]
    return {}


nav_gross_pr, nav_net_pr = build_nav_pr(all_dates, sh, rebal_dates, w_history)
print("   NAV gross PR: %.2f → %.2f" % (nav_gross_pr.iloc[0], nav_gross_pr.iloc[-1]))
print("   NAV net   PR: %.2f → %.2f" % (nav_net_pr.iloc[0],   nav_net_pr.iloc[-1]))

# ── Benchmark BRVM30 PR ───────────────────────────────────────────────────────
base_val = brvm30_raw[START_DATE]
bench_dict = {d: v / base_val * 100 for d, v in brvm30_raw.items()}

nav_bench = []
last_pr = 100.0
for dt in all_dates:
    if dt in bench_dict:
        last_pr = bench_dict[dt]
    nav_bench.append([dt, round(last_pr, 6)])

bench_s = pd.Series({d: v for d, v in nav_bench}, dtype=float)

# ── Métriques globales ────────────────────────────────────────────────────────
print("[4/6] Calcul des métriques globales PR…")

def compute_te_td(nav_etf_s, bench_s_):
    idx = nav_etf_s.index.intersection(bench_s_.index)
    e, b = nav_etf_s[idx], bench_s_[idx]
    if len(e) < 10:
        return 0.0, 0.0, 0.0
    r_e = e.pct_change().dropna()
    r_b = b.pct_change().dropna()
    ci  = r_e.index.intersection(r_b.index)
    te  = float((r_e[ci] - r_b[ci]).std(ddof=1) * np.sqrt(252))
    td  = float(e.iloc[-1] / b.iloc[-1] - 1)
    n_y = len(e) / 252
    td_ann = float((1 + td) ** (1 / n_y) - 1) if n_y > 0 else 0.0
    return te, td, td_ann

te_gross, td_gross, td_gross_ann = compute_te_td(nav_gross_pr, bench_s)
te_net,   td_net,   td_net_ann   = compute_te_td(nav_net_pr,   bench_s)
print("   TE gross=%.3f%%  TD gross=%.2f%%/an" % (te_gross*100, td_gross_ann*100))
print("   TE net=%.3f%%    TD net=%.2f%%/an"   % (te_net*100,   td_net_ann*100))

# Turnover moyen par rebalancement
def turnover_at_rebal(w_history, rebal_dates):
    turnovers = []
    for i in range(1, len(rebal_dates)):
        w_prev = _get_weights(w_history, rebal_dates[i-1])
        w_cur  = _get_weights(w_history, rebal_dates[i])
        all_tks = set(w_prev) | set(w_cur)
        to = sum(abs(w_cur.get(tk, 0) - w_prev.get(tk, 0)) for tk in all_tks) / 2
        turnovers.append(to)
    return float(np.mean(turnovers)) if turnovers else 0.0

to_avg = turnover_at_rebal(w_history, rebal_dates)

# Métriques annuelles
annual_new = []
for yr in [2023, 2024, 2025, 2026]:
    mask_e = nav_net_pr.index.astype(str).str.startswith(str(yr))
    mask_b = bench_s.index.astype(str).str.startswith(str(yr))

    e_yr = nav_net_pr[mask_e]
    g_yr = nav_gross_pr[mask_e]
    b_yr = bench_s[mask_b]
    if len(e_yr) < 5:
        continue

    te_yr, td_yr, _ = compute_te_td(e_yr, b_yr)
    te_gr, td_gr, _ = compute_te_td(g_yr, b_yr)
    old = next((a for a in bm.get('annual', []) if a.get('year') == yr), {})
    annual_new.append({
        'year':       yr,
        'te':         round(te_yr, 6),
        'td':         round(td_yr, 6),
        'td_gross':   round(td_gr, 6),
        'cost_tx':    old.get('cost_tx', 0),
        'mgmt_fee':   old.get('mgmt_fee', 0),
        'basket_gap': round(td_gr, 6),
    })
    print("   %d: TE=%.2f%%  TD=%.2f%%" % (yr, te_yr*100, td_yr*100))

# ── Mise à jour dashboard_data.json ──────────────────────────────────────────
print("[5/6] Mise à jour dashboard_data.json…")
dd['nav_bench']     = nav_bench
dd['nav_etf']       = [[d, round(v, 6)] for d, v in nav_net_pr.items()]
dd['nav_gross']     = [[d, round(v, 6)] for d, v in nav_gross_pr.items()]

m = dd.get('metrics', {})
m['te']              = round(te_net, 6)
m['te_gross']        = round(te_gross, 6)
m['td']              = round(td_net, 6)
m['td_ann']          = round(td_net_ann, 6)
m['td_gross']        = round(td_gross, 6)
m['td_gross_ann']    = round(td_gross_ann, 6)
m['div_bench_factor']= 1.0   # PR pur
m['div_etf_factor']  = 1.0   # PR pur
dd['metrics'] = m
json.dump(dd, open(DD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))

# Mise à jour backtest_metrics.json
n_years = len(nav_net_pr) / 252
bm['te_full']      = round(te_net, 6)
bm['te_prog']      = round(te_gross, 6)
bm['td_full']      = round(td_net, 6)
bm['td_full_ann']  = round(td_net_ann, 6)
bm['td_gross']     = round(td_gross, 6)
bm['td_gross_ann'] = round(td_gross_ann, 6)
bm['turnover_avg'] = round(to_avg, 6)
bm['annual']       = annual_new
json.dump(bm, open(BM_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

# ── Tests de validation ───────────────────────────────────────────────────────
print("[6/6] Génération des tests de validation…")

# ── 6a. Stress tests (scénarios de rebalancement) ────────────────────────────
def stress_scenario(name, rebal_freq_months, fee=MGMT_FEE_ANN):
    """Recalcule avec une fréquence de rebalancement différente."""
    # Sélectionner des dates de rebalancement tous les N mois
    dates_ts = pd.to_datetime(all_dates)
    rb_new = [START_DATE]
    last_ts = pd.Timestamp(START_DATE)
    for dt in all_dates[1:]:
        ts = pd.Timestamp(dt)
        if (ts.year - last_ts.year) * 12 + (ts.month - last_ts.month) >= rebal_freq_months:
            rb_new.append(dt)
            last_ts = ts
    if rb_new[-1] != END_DATE:
        rb_new.append(END_DATE)

    # Réutiliser les poids les plus proches de w_history
    wh_new = {}
    for rb in rb_new:
        # Trouver le rebalancement d'origine le plus proche
        closest = min(rebal_dates, key=lambda d: abs(pd.Timestamp(d) - pd.Timestamp(rb)))
        wh_new[rb] = _get_weights(w_history, closest)

    ng, nn = build_nav_pr(all_dates, sh, rb_new, wh_new, fee)
    te_s, td_s, _ = compute_te_td(nn, bench_s)

    # Turnover
    to_s = 0.0
    for i in range(1, len(rb_new)):
        wp = wh_new[rb_new[i-1]]; wc = wh_new[rb_new[i]]
        all_tks = set(wp) | set(wc)
        to_s += sum(abs(wc.get(tk,0) - wp.get(tk,0)) for tk in all_tks) / 2
    to_s /= max(1, len(rb_new)-1)

    return {'name': name, 'te': round(te_s,6), 'td': round(td_s,6), 'turnover': round(to_s,6)}

stress_tests = [
    stress_scenario('Trimestriel (référence)', 3),
    stress_scenario('Mensuel',                 1),
    stress_scenario('Semestriel',              6),
    stress_scenario('Annuel',                 12),
    stress_scenario('Trimestriel +frais ×2',   3, fee=MGMT_FEE_ANN*2),
    stress_scenario('Trimestriel 0-frais',      3, fee=0.0),
]
print("   Stress tests:", [s['name'] for s in stress_tests])

# ── 6b. Sensibilité seuil EWMA ────────────────────────────────────────────────
# Simuler différents seuils de turnover autorisé (proxy EWMA)
ewma_sensitivity = []
for threshold in [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25]:
    # Limite le turnover → moins de rebalancements effectués
    # On simule en sautant les rebalancements où le turnover calculé < threshold
    rb_filtered = [rebal_dates[0]]
    for i in range(1, len(rebal_dates)):
        wp = _get_weights(w_history, rebal_dates[i-1])
        wc = _get_weights(w_history, rebal_dates[i])
        all_tks = set(wp) | set(wc)
        to = sum(abs(wc.get(tk,0)-wp.get(tk,0)) for tk in all_tks)/2
        if to >= threshold:
            rb_filtered.append(rebal_dates[i])

    wh_f = {d: _get_weights(w_history, d) for d in rb_filtered}
    ng, nn = build_nav_pr(all_dates, sh, rb_filtered, wh_f)
    te_f, td_f, _ = compute_te_td(nn, bench_s)
    to_f = sum(
        sum(abs(_get_weights(w_history,rb_filtered[i]).get(tk,0) -
                _get_weights(w_history,rb_filtered[i-1]).get(tk,0)) for tk in
            set(_get_weights(w_history,rb_filtered[i])) |
            set(_get_weights(w_history,rb_filtered[i-1])))/2
        for i in range(1,len(rb_filtered))
    ) / max(1, len(rb_filtered)-1)
    ewma_sensitivity.append({'threshold': threshold, 'te': round(te_f,6),
                              'td': round(td_f,6), 'turnover': round(to_f,6),
                              'n_rebal': len(rb_filtered)-1})

# ── 6c. Bootstrap TE ─────────────────────────────────────────────────────────
r_e = nav_net_pr.pct_change().dropna()
r_b = bench_s.pct_change().dropna()
ci  = r_e.index.intersection(r_b.index)
diff_daily = (r_e[ci] - r_b[ci]).values
N_SIM = 500
te_boots = []
for _ in range(N_SIM):
    samp = np.random.choice(diff_daily, size=len(diff_daily), replace=True)
    te_boots.append(float(samp.std(ddof=1) * np.sqrt(252)))
boot = {
    'n_sim':   N_SIM,
    'te_med':  round(float(np.median(te_boots)), 6),
    'te_p5':   round(float(np.percentile(te_boots, 5)),  6),
    'te_p95':  round(float(np.percentile(te_boots, 95)), 6),
}
print("   Bootstrap: TE median=%.3f%%  [%.3f%% - %.3f%%]" % (
    boot['te_med']*100, boot['te_p5']*100, boot['te_p95']*100))

# ── 6d. Walk-Forward ─────────────────────────────────────────────────────────
wf_results = []
wf_splits = [
    ('Jan–Déc 2023', '2023-01-02', '2023-12-29', '2024-01-02', '2024-12-31'),
    ('Jan–Déc 2024', '2024-01-02', '2024-12-31', '2025-01-02', '2025-12-31'),
    ('Jan–Déc 2025', '2025-01-02', '2025-12-31', '2026-01-02', '2026-04-01'),
    ('2023–2024 IS / 2025 OOS', '2023-01-02', '2024-12-31', '2025-01-02', '2025-12-31'),
    ('2023–2025 IS / 2026 OOS', '2023-01-02', '2025-12-31', '2026-01-02', '2026-04-01'),
]
for label, is_s, is_e, oos_s, oos_e in wf_splits:
    # In-sample
    is_e_idx  = nav_net_pr[nav_net_pr.index <= is_e]
    is_s_idx  = is_e_idx[is_e_idx.index >= is_s]
    b_is      = bench_s[(bench_s.index >= is_s) & (bench_s.index <= is_e)]
    te_is, _, _ = compute_te_td(is_s_idx, b_is)

    # Out-of-sample
    oos_net = nav_net_pr[(nav_net_pr.index >= oos_s) & (nav_net_pr.index <= oos_e)]
    b_oos   = bench_s[(bench_s.index >= oos_s) & (bench_s.index <= oos_e)]
    if len(oos_net) < 5 or len(b_oos) < 5:
        continue
    te_oos, td_oos, _ = compute_te_td(oos_net, b_oos)
    wf_results.append({
        'label':     label,
        'te_is':     round(te_is,  6),
        'te_oos':    round(te_oos, 6),
        'td_oos':    round(td_oos, 6),
        'delta_te':  round(te_oos - te_is, 6),
    })
    print("   WF %s: IS=%.2f%% OOS=%.2f%%" % (label, te_is*100, te_oos*100))

# ── Sauvegarde validation_results.json ───────────────────────────────────────
vl = {
    'stress_tests':      stress_tests,
    'ewma_sensitivity':  ewma_sensitivity,
    'bootstrap':         boot,
    'walk_forward':      wf_results,
}
json.dump(vl, open(VL_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

# ── 6e. Scalabilité (différents AUM, impact sur coûts de transaction) ────────
# Paramètres BRVM: spread moyen ~0.5%, volume journalier moyen par titre ~10M FCFA
SPREAD_BPS   = 50      # coût de transaction en bps
VOL_DAY_FCFA = 10_000_000  # volume journalier moyen par titre en FCFA

# ETF initial : 50 000 parts × 10 000 FCFA = 500M FCFA
N_PARTS_BASE = 50_000
PRICE_BASE   = 10_000

aum_scenarios = [
    ('500M FCFA (actuel)',  500e6),
    ('1 Md FCFA',         1_000e6),
    ('2.5 Md FCFA',       2_500e6),
    ('5 Md FCFA',         5_000e6),
    ('10 Md FCFA',       10_000e6),
]

sc_results = []
for sc_name, aum in aum_scenarios:
    # Coût de transaction : spread × turnover × AUM
    cost_tx_ann  = (SPREAD_BPS / 10_000) * to_avg * 4  # 4 rebalancements/an
    cost_tx_cumul = cost_tx_ann * (len(all_dates) / 252)

    # Impact du market impact pour les gros AUM
    # Si AUM > 10% du volume journalier × 30 titres = 300M FCFA
    vol_total = VOL_DAY_FCFA * 27 * 252  # volume annuel total du panier
    market_impact_factor = max(0, (aum / vol_total - 0.005)) * 5
    te_sc = float(te_net * (1 + market_impact_factor))
    td_sc = float(td_net - cost_tx_cumul * market_impact_factor)

    sc_results.append({
        'scenario':      sc_name,
        'aum_fcfa':      aum,
        'te':            round(te_sc, 6),
        'td':            round(td_sc, 6),
        'turnover':      round(to_avg, 6),
        'cost_tx_cumul': round(cost_tx_cumul * (1 + market_impact_factor), 6),
        'basket_n_avg':  bm.get('n_titres_avg', 27),
    })
    print("   Scalabilité %s: TE=%.2f%%  TD=%.2f%%" % (sc_name, te_sc*100, td_sc*100))

json.dump(sc_results, open(SC_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

print()
print("=== DONE ===")
print("nav_etf_PR  : %.2f → %.2f  (TD vs BRVM30 PR = %+.2f%%)" % (
    nav_net_pr.iloc[0], nav_net_pr.iloc[-1], td_net*100))
print("nav_gross_PR: %.2f → %.2f  (TD vs BRVM30 PR = %+.2f%%)" % (
    nav_gross_pr.iloc[0], nav_gross_pr.iloc[-1], td_gross*100))
print("nav_bench PR: %.2f → %.2f" % (bench_s.iloc[0], bench_s.iloc[-1]))
print("TE nette : %.2f%%   TE brute : %.2f%%" % (te_net*100, te_gross*100))
