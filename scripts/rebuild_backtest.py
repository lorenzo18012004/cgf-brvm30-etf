"""
Reconstruction complète du backtest avec les nouvelles règles de sélection.

Pour chaque date de rebalancement historique :
  1. Reconstitue l'univers BRVM30 complet (panier + exclus de rebal_detail.json)
  2. Applique les 5 règles de sélection
  3. Construit le nouveau w_history
  4. Met à jour dashboard_data.json + backtest_metrics.json
  5. Lance run_backtest_validation.py pour recalculer toutes les métriques

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

# ── Paramètres de sélection ───────────────────────────────────────────────────
FORCE_WEIGHT        = 0.03    # ≥ 3% → forcé quoi qu'il arrive
STALE_THRESH        = 0.70    # ≥ 70% jours sans cotation → exclu (sauf forcé)
STALE_WINDOW        = 63      # fenêtre 3 mois (jours ouvrés)
MAX_EXEC_NEW_DAYS   = 100     # nouveau entrant : max 100j
MAX_EXEC_EXIST_DAYS = 32      # titre existant  : max 32j
CONSEC_REBALS_EXIT  = 2       # 2 rebals consécutifs > seuil → sortie
FLOAT_MIN_MFCFA     = 7_000  # float < 7 Md FCFA → exclu
MIN_BASKET_WEIGHT   = 0.001  # < 0.1% après redistribution → exclu
AUM_MFCFA           = 5_000  # AUM de référence pour calcul exec_days

print("[1/4] Chargement des données…")
sh = json.load(open(SH_PATH, encoding='utf-8'))
rd = json.load(open(RD_PATH, encoding='utf-8'))
dd = json.load(open(DD_PATH, encoding='utf-8'))

# ── Fonctions ADV et stale ────────────────────────────────────────────────────
def compute_adv(ticker, as_of_date, window=STALE_WINDOW):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    vals  = [(hist[d].get('volume') or 0) * (hist[d].get('close') or 0) / 1e6
             for d in dates if (hist[d].get('volume') or 0) > 0 and (hist[d].get('close') or 0) > 0]
    return float(np.mean(vals)) if vals else 0.0

def compute_stale(ticker, as_of_date, window=STALE_WINDOW):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    if not dates:
        return 1.0
    return sum(1 for d in dates if (hist[d].get('volume') or 0) == 0) / len(dates)

# ── Reconstituer l'univers BRVM30 à chaque rebalancement ─────────────────────
print("[2/4] Reconstitution des univers BRVM30 par rebalancement…")

rebals_src = [r for r in rd.get('rebalancings', []) if not r.get('skipped') or r.get('basket')]
rebal_dates = sorted(set(r['date'] for r in rebals_src))

# Pour chaque date : {ticker: {w_brvm30, adv_from_data, float_ok, raison_orig}}
universes = {}
for r in rebals_src:
    dt      = r['date']
    univ    = {}
    for item in r.get('basket', []) + r.get('excluded', []):
        tk = item.get('ticker')
        if not tk or tk in univ:
            continue
        float_exclu = 'Float' in str(item.get('raison', ''))
        univ[tk] = {
            'w_brvm30':    item.get('w_brvm30', 0.0),
            'adv_mfcfa':   item.get('adv_mfcfa', 0.0),
            'adv_req':     item.get('adv_req', 0.0),
            'secteur':     item.get('secteur', '—'),
            'float_exclu': float_exclu,   # float < 7 Md selon données orig
            'raison_orig': item.get('raison', ''),
        }
    if univ:
        universes[dt] = univ

print("   Rebalancements trouvés :", sorted(universes.keys()))

# ── Application des nouvelles règles ─────────────────────────────────────────
def _get_old_weights(wh, dt):
    val = wh.get(dt, {})
    if isinstance(val, dict):
        return val
    if isinstance(val, list) and len(val) == 2 and isinstance(val[1], dict):
        return val[1]
    return {}

print("[3/4] Application des nouvelles règles de sélection…")

new_w_history       = {}
new_excluded_by_date = {}
new_basket_by_date   = {}
excess_days_cnt     = {}
prev_basket         = set()

report_lines = []

for i, dt in enumerate(sorted(universes.keys())):
    univ = universes[dt]
    forced, included, excluded = [], [], []
    excess_new = dict(excess_days_cnt)

    for tk, info in univ.items():
        w_b30       = info['w_brvm30']
        adv_data    = info['adv_mfcfa']
        float_exclu = info['float_exclu']

        # Recalcul ADV depuis sika (plus précis que la donnée stockée)
        adv_live = compute_adv(tk, dt)
        adv = adv_live if adv_live > 0 else adv_data
        stale = compute_stale(tk, dt)

        trade_mfcfa = w_b30 * AUM_MFCFA
        exec_days   = trade_mfcfa / adv if adv > 0 else 999.0
        is_new      = tk not in prev_basket

        # ── Règle 4 : Float ───────────────────────────────────────────────
        if float_exclu:
            excluded.append((tk, w_b30, 'Float < 7 Md FCFA'))
            excess_new.pop(tk, None)
            continue

        # ── Règle 1 : Force si poids ≥ 3% (priorité sur stale et ADV) ────
        if w_b30 >= FORCE_WEIGHT:
            forced.append((tk, w_b30))
            excess_new.pop(tk, None)
            continue

        # ── Règle 2 : Prix stale ≥ 70% sur 3 mois ────────────────────────
        if stale >= STALE_THRESH:
            excluded.append((tk, w_b30, f'Stale {stale*100:.0f}% (3 mois)'))
            excess_new.pop(tk, None)
            continue

        # ── Règle 3 : ADV / jours d'exécution ────────────────────────────
        max_days = MAX_EXEC_NEW_DAYS if is_new else MAX_EXEC_EXIST_DAYS
        if exec_days > max_days:
            if is_new:
                excluded.append((tk, w_b30,
                    f'ADV insuffisant nouveau entrant : {exec_days:.0f}j > {max_days}j'))
                excess_new.pop(tk, None)
            else:
                excess_new[tk] = excess_new.get(tk, 0) + 1
                if excess_new[tk] >= CONSEC_REBALS_EXIT:
                    excluded.append((tk, w_b30,
                        f'ADV insuffisant {CONSEC_REBALS_EXIT} rebals consécutifs : {exec_days:.0f}j > {max_days}j'))
                    excess_new.pop(tk, None)
                else:
                    # Tolérance : maintenu encore ce rebal
                    included.append((tk, w_b30))
        else:
            excess_new.pop(tk, None)
            included.append((tk, w_b30))

    excess_days_cnt = excess_new

    # Assembler basket brut
    basket_raw = forced + included

    # ── Règle 5 : Poids minimum 0.1% ─────────────────────────────────────
    total_w = sum(w for _, w in basket_raw)
    final   = []
    for tk, w_b30 in basket_raw:
        w_norm = w_b30 / total_w if total_w > 0 else 1 / max(len(basket_raw), 1)
        if w_norm < MIN_BASKET_WEIGHT and w_b30 < FORCE_WEIGHT:
            excluded.append((tk, w_b30, f'Poids < {MIN_BASKET_WEIGHT*100:.1f}% après redistribution'))
        else:
            final.append((tk, w_b30))

    # Normalisation finale
    total_f = sum(w for _, w in final)
    basket_weights = {tk: round(w / total_f, 6) for tk, w in final} if total_f > 0 else {}

    new_w_history[dt] = basket_weights
    prev_basket = set(basket_weights.keys())

    # Sauvegarder les exclusions pour mise à jour de rebal_detail.json
    new_excluded_by_date[dt] = [
        {'ticker': tk, 'w_brvm30': round(w_b30, 6), 'raison': raison,
         'adv_mfcfa': round(compute_adv(tk, dt), 1),
         'stale_ratio': round(compute_stale(tk, dt), 3),
         'secteur': univ.get(tk, {}).get('secteur', '—')}
        for tk, w_b30, raison in excluded
    ]
    new_basket_by_date[dt] = [
        {'ticker': tk,
         'w_etf': basket_weights[tk],
         'w_brvm30': round(univ.get(tk, {}).get('w_brvm30', 0), 6),
         'force': univ.get(tk, {}).get('w_brvm30', 0) >= FORCE_WEIGHT,
         'adv_mfcfa': round(compute_adv(tk, dt), 1),
         'stale_ratio': round(compute_stale(tk, dt), 3),
         'secteur': univ.get(tk, {}).get('secteur', '—')}
        for tk in basket_weights
    ]

    # Rapport
    n_forced  = sum(1 for tk, w in forced   if tk in basket_weights)
    n_incl    = sum(1 for tk, w in included if tk in basket_weights)
    n_excl    = len(excluded)
    cov       = sum(info['w_brvm30'] for tk, info in univ.items() if tk in basket_weights)
    report_lines.append({
        'Date': dt,
        'Panier': len(basket_weights),
        'Forcés': n_forced,
        'Inclus ADV OK': n_incl,
        'Exclus': n_excl,
        'Couverture indice': f'{cov*100:.1f}%',
    })

    print(f"   {dt}: {len(basket_weights)} titres ({n_forced} forcés) — "
          f"couverture {cov*100:.1f}% — {n_excl} exclus")
    for tk, w_b30, raison in sorted(excluded, key=lambda x: -x[1]):
        tag = ' [NOUVEAU vs ancien]' if raison != universes[dt].get(tk, {}).get('raison_orig', '') else ''
        print(f"      EXCLU {tk} ({w_b30*100:.2f}%) : {raison}{tag}")

# Comparer avec l'ancien w_history
old_wh = dd.get('w_history', {})
print()
print("=== Comparaison ancien vs nouveau panier ===")
for dt in sorted(new_w_history.keys()):
    old_tks = set(_get_old_weights(old_wh, dt).keys()) if dt in old_wh else set()
    new_tks = set(new_w_history[dt].keys())
    entrants = new_tks - old_tks
    sortants  = old_tks - new_tks
    if entrants or sortants:
        print(f"  {dt}: +{entrants or '{}'} / -{sortants or '{}'}")
    else:
        print(f"  {dt}: identique ({len(new_tks)} titres)")

def _get_old_weights(wh, dt):
    val = wh.get(dt, {})
    if isinstance(val, dict):
        return val
    if isinstance(val, list) and len(val) == 2 and isinstance(val[1], dict):
        return val[1]
    return {}

# ── Mise à jour dashboard_data.json ──────────────────────────────────────────
print()
print("[4/4] Mise à jour dashboard_data.json et rebal_detail.json…")

# Mise à jour w_history dans dashboard_data.json
dd['w_history'] = new_w_history
json.dump(dd, open(DD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print("   w_history mis à jour —", len(new_w_history), "rebalancements")

# Mise à jour rebal_detail.json avec les nouvelles exclusions et basket
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

# ── Relancer la validation complète ──────────────────────────────────────────
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
