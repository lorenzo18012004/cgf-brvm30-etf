"""
Reconstruction complète du backtest avec contrainte de liquidité ADV.

Méthodologie :
  1. Poids cible = capitalisation totale Sika (répliquer le BRVM30)
  2. Chaque titre est plafonné à ce que son ADV permet d'exécuter :
       poids_max = ADV × MAX_EXEC_DAYS / AUM
  3. L'excès est redistribué proportionnellement aux titres non plafonnés
  4. Itération jusqu'à stabilité
  5. Les titres sans ADV (jamais cotés) sont exclus

Plus de règle float, plus de règle stale : l'ADV capture naturellement
l'illiquidité — un titre stale a ADV ≈ 0, donc poids_max ≈ 0.

Usage : python scripts/rebuild_backtest.py
"""
import sys, os, json, subprocess
import numpy as np
import pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')
SH_PATH = os.path.join(DATA, 'sika_history.json')
RD_PATH = os.path.join(DATA, 'rebal_detail.json')
DD_PATH = os.path.join(DATA, 'dashboard_data.json')

# ── Paramètres ────────────────────────────────────────────────────────────────
MAX_EXEC_SMALL  = 32      # jours max pour petits titres (<3% BRVM30)
MAX_EXEC_LARGE  = 62      # jours max pour grands titres (≥3% BRVM30, OTC possible)
LARGE_THRESHOLD = 0.03    # seuil "grand titre" (3%)
AUM_MFCFA       = 5_000  # AUM de référence en M FCFA
MIN_ADV_MFCFA   = 0.5    # ADV minimum pour être inclus (500k FCFA/j)
MIN_WEIGHT      = 0.001  # poids minimum après redistribution (0.1%)
STALE_WINDOW    = 63     # fenêtre ADV en jours ouvrés

print("[1/4] Chargement des données…")
sh = json.load(open(SH_PATH, encoding='utf-8'))
rd = json.load(open(RD_PATH, encoding='utf-8'))
dd = json.load(open(DD_PATH, encoding='utf-8'))

# ── Calcul ADV ────────────────────────────────────────────────────────────────
def compute_adv(ticker, as_of_date, window=STALE_WINDOW):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    vals  = [(hist[d].get('volume') or 0) * (hist[d].get('close') or 0) / 1e6
             for d in dates]
    return float(sum(vals) / len(dates)) if dates else 0.0

def compute_stale(ticker, as_of_date, window=STALE_WINDOW):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    if not dates:
        return 1.0
    return sum(1 for d in dates if (hist[d].get('volume') or 0) == 0) / len(dates)

# ── Algorithme ADV-cap + redistribution ──────────────────────────────────────
def build_adv_capped_weights(univ, aum, max_days_small, max_days_large,
                              large_thresh, min_adv, min_w):
    """
    Tous les titres sont soumis au plafond ADV — aucune force rule :
      - Grand titre (w_brvm30 >= large_thresh=3%) : max = ADV × 62j / AUM
      - Petit titre (w_brvm30 <  large_thresh=3%) : max = ADV × 32j / AUM
    L'excès au-delà du plafond est redistribué proportionnellement aux non-plafonnés.
    Titres sans ADV suffisant (< min_adv) : exclus.
    Titres dont le poids résultant < min_w (0.1%) : exclus + redistribués.

    Retourne ({ticker: poids_final}, {ticker: (raison, w_brvm30)})
    """
    adv = {tk: info.get('adv_live', 0) or info.get('adv_mfcfa', 0)
           for tk, info in univ.items()}

    # Filtrer les titres sans ADV suffisant
    eligible_tks = [tk for tk in univ if adv[tk] >= min_adv]
    exclu_tks    = [tk for tk in univ if adv[tk] < min_adv]

    if not eligible_tks:
        return {}, {tk: (f'ADV {adv[tk]:.1f} < {min_adv} MFCFA', univ[tk]['w_brvm30'])
                    for tk in exclu_tks}

    # Poids initiaux = w_brvm30 normalisés aux éligibles
    total_init = sum(univ[tk]['w_brvm30'] for tk in eligible_tks)
    weights = {tk: univ[tk]['w_brvm30'] / total_init for tk in eligible_tks}

    # Plafond ADV par titre (62j pour grands, 32j pour petits)
    max_w = {}
    for tk in eligible_tks:
        max_days = max_days_large if univ[tk]['w_brvm30'] >= large_thresh else max_days_small
        max_w[tk] = min(adv[tk] * max_days / aum, 1.0)

    # Itération plafonner + redistribuer
    for _ in range(50):
        capped   = {tk for tk in eligible_tks if weights[tk] > max_w[tk]}
        uncapped = [tk for tk in eligible_tks if tk not in capped]
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

    # Exclure poids < min_w (0.1%) et redistribuer
    for _ in range(10):
        tiny = [tk for tk in eligible_tks if 0 < weights[tk] < min_w]
        if not tiny:
            break
        for tk in tiny:
            exclu_tks.append(tk)
            eligible_tks.remove(tk)
        if not eligible_tks:
            break
        total_keep = sum(weights[tk] for tk in eligible_tks)
        for tk in eligible_tks:
            weights[tk] = weights[tk] / total_keep if total_keep > 0 else 1 / len(eligible_tks)

    # Normalisation finale
    final = {tk: weights[tk] for tk in eligible_tks}
    total = sum(final.values())
    if total > 0:
        final = {tk: round(v / total, 6) for tk, v in final.items()}

    # Raisons d'exclusion
    exclu_info = {}
    for tk in exclu_tks:
        a = adv[tk]
        if a < min_adv:
            exclu_info[tk] = (f'ADV {a:.1f} MFCFA < {min_adv} MFCFA', univ[tk]['w_brvm30'])
        else:
            exclu_info[tk] = (f'Poids < {min_w*100:.1f}% après redistribution', univ[tk]['w_brvm30'])

    return final, exclu_info


# ── Reconstituer l'univers BRVM30 à chaque rebalancement ─────────────────────
print("[2/4] Reconstitution des univers BRVM30 par rebalancement…")

rebals_src  = [r for r in rd.get('rebalancings', []) if not r.get('skipped') or r.get('basket')]
rebal_dates = sorted(set(r['date'] for r in rebals_src))

universes = {}
for r in rebals_src:
    dt   = r['date']
    univ = {}
    for item in r.get('basket', []) + r.get('excluded', []):
        tk = item.get('ticker')
        if not tk or tk in univ:
            continue
        univ[tk] = {
            'w_brvm30': item.get('w_brvm30', 0.0),
            'adv_mfcfa': item.get('adv_mfcfa', 0.0),
            'secteur': item.get('secteur', '—'),
        }
    if univ:
        universes[dt] = univ

print("   Rebalancements trouvés :", sorted(universes.keys()))

# ── Application de la contrainte ADV ─────────────────────────────────────────
print("[3/4] Application de la contrainte liquidité ADV…")

new_w_history        = {}
new_excluded_by_date = {}
new_basket_by_date   = {}
report_lines         = []

def _get_old_weights(wh, dt):
    val = wh.get(dt, {})
    if isinstance(val, dict): return val
    if isinstance(val, list) and len(val) == 2 and isinstance(val[1], dict): return val[1]
    return {}

for dt in sorted(universes.keys()):
    univ = universes[dt]

    # Recalculer ADV live depuis Sika (plus précis)
    for tk in univ:
        adv_live = compute_adv(tk, dt)
        univ[tk]['adv_live'] = adv_live if adv_live > 0 else univ[tk].get('adv_mfcfa', 0)

    basket_w, exclu_info = build_adv_capped_weights(
        univ, AUM_MFCFA, MAX_EXEC_SMALL, MAX_EXEC_LARGE,
        LARGE_THRESHOLD, MIN_ADV_MFCFA, MIN_WEIGHT
    )

    new_w_history[dt] = basket_w

    # Rapport
    cov   = sum(univ[tk]['w_brvm30'] for tk in basket_w)
    total_w_all = sum(univ[tk]['w_brvm30'] for tk in univ)
    cov_pct = cov / total_w_all if total_w_all > 0 else 0

    exclu_list  = sorted(exclu_info.items(), key=lambda x: -x[1][1])
    n_excl = len(exclu_list)

    # Compter plafonnés (poids ETF < poids brvm30 normalisé aux éligibles)
    total_brvm30_eligible = sum(univ[tk]['w_brvm30'] for tk in basket_w)
    n_plafonnes = sum(
        1 for tk in basket_w
        if total_brvm30_eligible > 0 and
           basket_w[tk] < round(univ[tk]['w_brvm30'] / total_brvm30_eligible, 6) - 5e-5
    )

    print(f"   {dt}: {len(basket_w)} titres ({n_plafonnes} plafonnés ADV) — "
          f"couverture {cov_pct*100:.1f}% — {n_excl} exclus")
    for tk, (raison, w_b30) in exclu_list:
        print(f"      EXCLU {tk} ({w_b30*100:.2f}%) : {raison}")

    report_lines.append({
        'Date': dt,
        'Panier': len(basket_w),
        'Plafonnes ADV': n_plafonnes,
        'Exclus': n_excl,
        'Couverture': f'{cov_pct*100:.1f}%',
    })

    new_excluded_by_date[dt] = [
        {'ticker': tk, 'w_brvm30': round(w_b30, 6), 'raison': raison,
         'adv_mfcfa': round(univ.get(tk, {}).get('adv_live', 0), 1),
         'stale_ratio': round(compute_stale(tk, dt), 3),
         'secteur': univ.get(tk, {}).get('secteur', '—')}
        for tk, (raison, w_b30) in exclu_info.items()
    ]
    new_basket_by_date[dt] = [
        {'ticker': tk,
         'w_etf': basket_w[tk],
         'w_brvm30': round(univ.get(tk, {}).get('w_brvm30', 0), 6),
         'force': False,
         'adv_mfcfa': round(univ.get(tk, {}).get('adv_live', 0), 1),
         'stale_ratio': round(compute_stale(tk, dt), 3),
         'secteur': univ.get(tk, {}).get('secteur', '—')}
        for tk in basket_w
    ]

# ── Comparaison avec l'ancien w_history ──────────────────────────────────────
old_wh = dd.get('w_history', {})
print()
print("=== Comparaison ancien vs nouveau panier ===")
for dt in sorted(new_w_history.keys()):
    old_tks = set(_get_old_weights(old_wh, dt).keys()) if dt in old_wh else set()
    new_tks = set(new_w_history[dt].keys())
    entrants = new_tks - old_tks
    sortants  = old_tks - new_tks
    if entrants or sortants:
        print(f"  {dt}: +{sorted(entrants) or '{}'} / -{sorted(sortants) or '{}'}")
    else:
        print(f"  {dt}: identique ({len(new_tks)} titres)")

# ── Mise à jour des fichiers ──────────────────────────────────────────────────
print()
print("[4/4] Mise à jour dashboard_data.json et rebal_detail.json…")

dd['w_history'] = new_w_history
json.dump(dd, open(DD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print("   w_history mis à jour —", len(new_w_history), "rebalancements")

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
print("=== Tableau récapitulatif des rebalancements ===")
df = pd.DataFrame(report_lines)
print(df.to_string(index=False))
