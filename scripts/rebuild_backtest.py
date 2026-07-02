"""
Reconstruction complète du backtest avec les règles live actuelles.

Méthodologie (identique à rebalance_live.py) :
  1. Poids cible = capitalisation totale Sika (répliquer le BRVM30)
  2. Top FORCE_TOP_N par poids indice → OTC, tenus à leur poids exact
  3. Restants : cap sur le DELTA depuis l'ancien panier
       max_delta = PARTICIPATION_RATE × ADV × max_days / AUM
  4. Redistribution itérative : excédent des capés → non-capés non-top5 uniquement
  5. ADV = moyenne sur le trimestre calendaire précédant la date de rebal

Usage : python scripts/rebuild_backtest.py
"""
import sys, os, json, subprocess, calendar
import numpy as np
import pandas as pd
from datetime import date as date_cls

sys.stdout.reconfigure(encoding='utf-8')

BASE    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA    = os.path.join(BASE, 'data')
SH_PATH = os.path.join(DATA, 'sika_history.json')
RD_PATH = os.path.join(DATA, 'rebal_detail.json')
DD_PATH = os.path.join(DATA, 'dashboard_data.json')

# ── Paramètres (identiques à rebalance_live.py) ───────────────────────────────
MAX_EXEC_SMALL     = 32
MAX_EXEC_LARGE     = 62
LARGE_THRESHOLD    = 0.03
PARTICIPATION_RATE = 0.15
FORCE_TOP_N        = 5
MIN_ADV_MFCFA      = 0.5
MIN_WEIGHT         = 0.001
AUM_MFCFA          = 5_000

print("[1/4] Chargement des données…")
sh = json.load(open(SH_PATH, encoding='utf-8'))
rd = json.load(open(RD_PATH, encoding='utf-8'))
dd = json.load(open(DD_PATH, encoding='utf-8'))

# ── ADV sur trimestre précédent ───────────────────────────────────────────────
def _prev_quarter_range(as_of_date_str):
    d = date_cls.fromisoformat(as_of_date_str)
    q = (d.month - 1) // 3
    if q == 0:
        start = date_cls(d.year - 1, 10, 1)
        end   = date_cls(d.year - 1, 12, 31)
    else:
        sm    = (q - 1) * 3 + 1
        em    = q * 3
        start = date_cls(d.year, sm, 1)
        end   = date_cls(d.year, em, calendar.monthrange(d.year, em)[1])
    return start.isoformat(), end.isoformat()

def compute_adv(ticker, as_of_date):
    q_start, q_end = _prev_quarter_range(as_of_date)
    hist  = sh.get(ticker, {})
    dates = [d for d in hist if q_start <= d <= q_end]
    vals  = [(hist[d].get('volume') or 0) * (hist[d].get('close') or 0) / 1e6
             for d in dates]
    return float(sum(vals) / len(dates)) if dates else 0.0

def compute_stale(ticker, as_of_date, window=63):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    if not dates:
        return 1.0
    return sum(1 for d in dates if (hist[d].get('volume') or 0) == 0) / len(dates)

# ── Algorithme ADV-cap identique à rebalance_live.py ─────────────────────────
def build_adv_capped_weights(w_brvm30, rebal_date, aum_mfcfa, adv_map, old_basket=None):
    """
    Même logique que rebalance_live.py :
    - Top FORCE_TOP_N (par poids indice) → OTC, tenus à leur poids exact
    - Autres : cap sur le delta depuis old_basket
    - Redistribution itérative : excédent → non-capés non-top5 uniquement
    """
    if old_basket is None:
        old_basket = {}

    total_brvm30 = sum(w_brvm30.values()) or 1.0
    w_norm = {tk: v / total_brvm30 for tk, v in w_brvm30.items()}

    exclu_info = {tk: f'ADV {adv_map[tk]:.1f} MFCFA < {MIN_ADV_MFCFA}'
                  for tk in w_norm if adv_map.get(tk, 0) < MIN_ADV_MFCFA}
    eligible = [tk for tk in w_norm if adv_map.get(tk, 0) >= MIN_ADV_MFCFA]
    if not eligible:
        return {}, exclu_info, set()

    total_elig = sum(w_norm[tk] for tk in eligible) or 1.0
    w_target   = {tk: w_norm[tk] / total_elig for tk in eligible}

    # Top FORCE_TOP_N par poids → OTC
    sorted_by_w = sorted(eligible, key=lambda tk: -w_target[tk])
    otc_set     = set(sorted_by_w[:FORCE_TOP_N])

    weights = {tk: w_target[tk] for tk in eligible}

    # Redistribution itérative : excédent des capés → non-capés non-top5
    for _ in range(30):
        capped_w = {}
        uncapped = []
        for tk in eligible:
            if tk in otc_set:
                continue
            w_cur     = old_basket.get(tk, 0.0)
            delta     = weights[tk] - w_cur
            max_d     = MAX_EXEC_LARGE if w_norm[tk] >= LARGE_THRESHOLD else MAX_EXEC_SMALL
            max_delta = PARTICIPATION_RATE * adv_map.get(tk, 0) * max_d / aum_mfcfa
            if abs(delta) > max_delta + 1e-6:
                capped_w[tk] = max(0.0, w_cur + (max_delta if delta > 0 else -max_delta))
            else:
                uncapped.append(tk)
        if not capped_w:
            break
        total_top5   = sum(w_target[tk] for tk in otc_set if tk in weights)
        total_capped = sum(capped_w[tk] for tk in capped_w)
        avail        = 1.0 - total_top5 - total_capped
        for tk in capped_w:
            weights[tk] = capped_w[tk]
        uncapped_tgt = sum(w_target[tk] for tk in uncapped) or 1.0
        for tk in uncapped:
            weights[tk] = max(0.0, avail * w_target[tk] / uncapped_tgt)
        for tk in otc_set:
            if tk in weights:
                weights[tk] = w_target[tk]

    # Exclusion des poids trop petits
    for _ in range(10):
        tiny = [tk for tk in eligible if 0 < weights.get(tk, 0) < MIN_WEIGHT and tk not in otc_set]
        if not tiny:
            break
        for tk in tiny:
            exclu_info[tk] = f'Poids < {MIN_WEIGHT*100:.1f}% après redistribution'
            eligible.remove(tk)
        if not eligible:
            break
        total_keep = sum(weights[tk] for tk in eligible) or 1.0
        for tk in eligible:
            weights[tk] = weights[tk] / total_keep

    final = {tk: round(weights[tk], 6) for tk in eligible if weights.get(tk, 0) > 0}

    # Normalisation finale : top-5 exactement à leur poids cible
    top5_total = sum(final[tk] for tk in otc_set if tk in final)
    rest_total = sum(final[tk] for tk in final if tk not in otc_set)
    if rest_total > 0:
        scale = (1.0 - top5_total) / rest_total
        final = {tk: (round(v, 6) if tk in otc_set else round(v * scale, 6))
                 for tk, v in final.items()}

    return final, exclu_info, otc_set


# ── Reconstitution des univers BRVM30 par rebalancement ──────────────────────
print("[2/4] Reconstitution des univers BRVM30 par rebalancement…")

rebals_src = [r for r in rd.get('rebalancings', []) if not r.get('skipped') or r.get('basket')]
rebals_src = sorted(rebals_src, key=lambda r: r['date'])

universes = {}
for r in rebals_src:
    dt, univ = r['date'], {}
    for item in r.get('basket', []) + r.get('excluded', []):
        tk = item.get('ticker')
        if not tk or tk in univ:
            continue
        univ[tk] = {'w_brvm30': item.get('w_brvm30', 0.0)}
    if univ:
        universes[dt] = univ

print("   Rebalancements trouvés :", sorted(universes.keys()))

# ── Application de la contrainte ADV avec suivi du panier précédent ──────────
print("[3/4] Application contrainte liquidité ADV (règles live)…")

new_w_history        = {}
new_excluded_by_date = {}
new_basket_by_date   = {}
report_lines         = []
old_basket_w         = {}   # panier précédent (simulé séquentiellement)

for dt in sorted(universes.keys()):
    univ = universes[dt]

    # Calcul ADV sur trimestre précédent pour chaque titre
    adv_map = {tk: compute_adv(tk, dt) for tk in univ}

    basket_w, exclu_info, otc_set = build_adv_capped_weights(
        w_brvm30    = {tk: info['w_brvm30'] for tk, info in univ.items()},
        rebal_date  = dt,
        aum_mfcfa   = AUM_MFCFA,
        adv_map     = adv_map,
        old_basket  = old_basket_w,
    )

    new_w_history[dt] = basket_w
    old_basket_w      = dict(basket_w)  # panier de ce rebal → référence du suivant

    # Rapport
    w_brvm30_map    = {tk: info['w_brvm30'] for tk, info in univ.items()}
    total_brvm30    = sum(w_brvm30_map.values()) or 1.0
    w_norm_map      = {tk: v / total_brvm30 for tk, v in w_brvm30_map.items()}
    total_elig_w    = sum(w_norm_map[tk] for tk in basket_w) or 1.0
    w_target_map    = {tk: w_norm_map[tk] / total_elig_w for tk in basket_w}

    n_capped = sum(
        1 for tk in basket_w
        if tk not in otc_set and basket_w[tk] < w_target_map.get(tk, 0) - 1e-4
    )
    cov_pct = sum(w_brvm30_map.get(tk, 0) for tk in basket_w) / total_brvm30

    print(f"   {dt}: {len(basket_w)} titres "
          f"({len(otc_set)} OTC, {n_capped} CAP) — "
          f"couverture {cov_pct*100:.1f}% — {len(exclu_info)} exclus")
    for tk, raison in sorted(exclu_info.items(), key=lambda x: -w_brvm30_map.get(x[0], 0)):
        print(f"      EXCLU {tk} ({w_brvm30_map.get(tk,0)*100:.2f}%) : {raison}")

    report_lines.append({
        'Date':        dt,
        'Panier':      len(basket_w),
        'OTC':         len(otc_set),
        'CAP':         n_capped,
        'Exclus':      len(exclu_info),
        'Couverture':  f'{cov_pct*100:.1f}%',
    })

    q_start, q_end = _prev_quarter_range(dt)
    new_excluded_by_date[dt] = [
        {
            'ticker':      tk,
            'w_brvm30':    round(w_brvm30_map.get(tk, 0), 6),
            'raison':      raison,
            'adv_mfcfa':   round(adv_map.get(tk, 0), 1),
            'stale_ratio': round(compute_stale(tk, dt), 3),
        }
        for tk, raison in exclu_info.items()
    ]
    new_basket_by_date[dt] = [
        {
            'ticker':      tk,
            'w_etf':       basket_w[tk],
            'w_brvm30':    round(w_brvm30_map.get(tk, 0), 6),
            'force_otc':   tk in otc_set,
            'capped':      tk not in otc_set and basket_w[tk] < w_target_map.get(tk, 0) - 1e-4,
            'adv_mfcfa':   round(adv_map.get(tk, 0), 1),
            'stale_ratio': round(compute_stale(tk, dt), 3),
            'adv_period':  f'{q_start} → {q_end}',
        }
        for tk in basket_w
    ]

# ── Comparaison ancien vs nouveau ────────────────────────────────────────────
old_wh = dd.get('w_history', {})
print()
print("=== Comparaison ancien vs nouveau panier ===")
for dt in sorted(new_w_history.keys()):
    old_tks = set((old_wh.get(dt) or {}).keys())
    new_tks = set(new_w_history[dt].keys())
    entrants = new_tks - old_tks
    sortants  = old_tks - new_tks
    if entrants or sortants:
        print(f"  {dt}: +{sorted(entrants) or '∅'} / -{sorted(sortants) or '∅'}")
    else:
        print(f"  {dt}: composition identique ({len(new_tks)} titres)")

# ── Mise à jour des fichiers ──────────────────────────────────────────────────
print()
print("[4/4] Mise à jour dashboard_data.json et rebal_detail.json…")

dd['w_history'] = new_w_history
json.dump(dd, open(DD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print(f"   w_history mis à jour — {len(new_w_history)} rebalancements")

for r in rd.get('rebalancings', []):
    dt = r.get('date')
    if dt in new_basket_by_date:
        r['basket']   = new_basket_by_date[dt]
        r['excluded'] = new_excluded_by_date[dt]
        r['basket_n'] = len(new_basket_by_date[dt])
        r['excl_n']   = len(new_excluded_by_date[dt])
        r['excl_w']   = round(sum(e['w_brvm30'] for e in new_excluded_by_date[dt]), 4)
        r['coverage'] = round(sum(b['w_brvm30'] for b in new_basket_by_date[dt]), 4)

json.dump(rd, open(RD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print("   rebal_detail.json mis à jour")

print()
print("Lancement de run_backtest_validation.py…")
result = subprocess.run(
    [sys.executable, os.path.join(BASE, 'scripts', 'run_backtest_validation.py')],
    capture_output=False, text=True
)
if result.returncode != 0:
    print("[ERREUR] run_backtest_validation.py a échoué")
    sys.exit(1)

print()
print("=== Tableau récapitulatif ===")
print(pd.DataFrame(report_lines).to_string(index=False))
