"""
recalibrate_history.py
======================
Rejoue tous les rebalancements historiques avec les nouvelles règles :
  - MAX_EXEC_LARGE = 40j de bourse (etait 62)
  - MAX_EXEC_SMALL = 20j de bourse (etait 32)
  - Sortants (w_new=0) : pas de cap, on liquide entierement

Met a jour :
  - data/dashboard_data.json  -> w_history
  - data/rebal_detail.json    -> basket par rebalancement
NE TOUCHE PAS nav_latest.json (etat live courant).
"""
import json, os, sys, calendar
import numpy as np
import pandas as pd
from datetime import date as date_cls
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')

# ── Parametres (alignes avec rebalance_live.py mis a jour) ────────────────────
MAX_EXEC_LARGE   = 40
MAX_EXEC_SMALL   = 20
LARGE_THRESHOLD  = 0.03
PARTICIPATION_RATE = 0.15
MIN_ADV_MFCFA    = 0.5
MIN_WEIGHT       = 0.001
FORCE_TOP_N      = 5
CASH_BUFFER      = 0.01
AUM_MFCFA        = 5_000

# ── Chargement ────────────────────────────────────────────────────────────────
sh   = json.load(open(os.path.join(DATA, 'sika_history.json'),              encoding='utf-8'))
soc  = json.load(open(os.path.join(DATA, 'sika_societe.json'),              encoding='utf-8'))
bch  = json.load(open(os.path.join(DATA, 'brvm_composition_history.json'), encoding='utf-8'))
dd   = json.load(open(os.path.join(DATA, 'dashboard_data.json'),           encoding='utf-8'))
rd   = json.load(open(os.path.join(DATA, 'rebal_detail.json'),             encoding='utf-8'))

# ── Helpers (copies de rebalance_live.py) ─────────────────────────────────────
def last_price(sh, ticker, as_of_date):
    hist = sh.get(ticker, {})
    past = sorted(d for d in hist if d <= as_of_date)
    if past:
        p = hist[past[-1]]
        close = p.get('close') if isinstance(p, dict) else p
        if close and float(close) > 0:
            return float(close)
    return None

def _prev_quarter_range(as_of_date):
    d = date_cls.fromisoformat(as_of_date)
    q = (d.month - 1) // 3
    if q == 0:
        start = date_cls(d.year - 1, 10, 1)
        end   = date_cls(d.year - 1, 12, 31)
    else:
        sm = (q - 1) * 3 + 1
        em = q * 3
        start = date_cls(d.year, sm, 1)
        end   = date_cls(d.year, em, calendar.monthrange(d.year, em)[1])
    return start.isoformat(), end.isoformat()

def compute_adv(sh, ticker, as_of_date):
    q_start, q_end = _prev_quarter_range(as_of_date)
    hist  = sh.get(ticker, {})
    dates = [d for d in hist if q_start <= d <= q_end]
    vals  = [(hist[d].get('volume') or 0) * (hist[d].get('close') or 0) / 1e6
             if isinstance(hist[d], dict) else 0
             for d in dates]
    return float(sum(vals) / len(dates)) if dates else 0.0

def get_total_cap_weights(tickers, rebal_date, sh, soc):
    market_cap = {}
    missing = []
    for tk in tickers:
        nb   = soc.get(tk, {}).get('nb_titres')
        prix = last_price(sh, tk, rebal_date)
        if nb and prix:
            market_cap[tk] = nb * prix
        else:
            missing.append(tk)
    if missing and market_cap:
        avg = sum(market_cap.values()) / len(market_cap)
        for tk in missing:
            market_cap[tk] = avg
    total = sum(market_cap.values())
    if total <= 0:
        return {tk: 1 / len(tickers) for tk in tickers}
    return {tk: market_cap[tk] / total for tk in tickers}

def build_adv_capped_weights(w_brvm30, rebal_date, aum_mfcfa, sh, old_basket=None):
    if old_basket is None:
        old_basket = {}
    total_brvm30 = sum(w_brvm30.values()) or 1.0
    w_norm = {tk: v / total_brvm30 for tk, v in w_brvm30.items()}
    adv    = {tk: compute_adv(sh, tk, rebal_date) for tk in w_norm}

    exclu_info = {tk: f'ADV {adv[tk]:.1f} MFCFA < {MIN_ADV_MFCFA}'
                  for tk in w_norm if adv[tk] < MIN_ADV_MFCFA}
    eligible = [tk for tk in w_norm if adv[tk] >= MIN_ADV_MFCFA]
    if not eligible:
        return {}, exclu_info, set()

    total_elig = sum(w_norm[tk] for tk in eligible) or 1.0
    w_target   = {tk: w_norm[tk] / total_elig for tk in eligible}
    sorted_by_w = sorted(eligible, key=lambda tk: -w_target[tk])
    otc_set = set(sorted_by_w[:FORCE_TOP_N])
    weights = {tk: w_target[tk] for tk in eligible}

    for _ in range(30):
        capped_w = {}
        uncapped = []
        for tk in eligible:
            if tk in otc_set:
                continue
            w_cur   = old_basket.get(tk, 0.0)
            delta   = weights[tk] - w_cur
            w_new_target = weights[tk]
            is_full_exit = (w_new_target == 0.0)
            if is_full_exit:
                uncapped.append(tk)
                continue
            max_d     = MAX_EXEC_LARGE if w_norm[tk] >= LARGE_THRESHOLD else MAX_EXEC_SMALL
            max_delta = PARTICIPATION_RATE * adv[tk] * max_d / aum_mfcfa
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

    for _ in range(5):
        tiny = [tk for tk in eligible if tk not in otc_set
                and 0 < weights.get(tk, 0) < MIN_WEIGHT]
        if not tiny:
            break
        for tk in tiny:
            exclu_info[tk] = f'Poids < {MIN_WEIGHT*100:.1f}%'
            eligible.remove(tk)
        if not eligible:
            break
        total_keep = sum(weights[tk] for tk in eligible) or 1.0
        weights = {tk: weights[tk] / total_keep for tk in eligible}
        for tk in otc_set:
            if tk in weights:
                weights[tk] = w_target[tk]

    final = {tk: round(weights[tk], 6) for tk in eligible if weights.get(tk, 0) > 0}
    top5_total = sum(final[tk] for tk in otc_set if tk in final)
    rest_total = sum(final[tk] for tk in final if tk not in otc_set)
    if rest_total > 0:
        scale = (1.0 - top5_total) / rest_total
        final = {tk: (round(v, 6) if tk in otc_set else round(v * scale, 6))
                 for tk, v in final.items()}
    return final, exclu_info, otc_set

# ── Replay chronologique ───────────────────────────────────────────────────────
rebals_sorted = sorted(
    [r for r in rd['rebalancings'] if not r.get('skipped')],
    key=lambda r: r['date']
)

w_history_new = {}
rebal_map = {r['date']: r for r in rd['rebalancings']}

old_basket_w = {}  # portfolio avant le 1er rebal = vide

for rb in rebals_sorted:
    dt = rb['date']
    print(f'\n=== {dt} ===')

    # Composition BRVM30 la plus recente avant ou egale a dt
    comp_entries = [c for c in bch if c.get('rebal_date') and
                    c['rebal_date'] <= dt and len(c.get('composition', [])) >= 25]
    if not comp_entries:
        print(f'  [SKIP] pas de composition disponible')
        w_history_new[dt] = old_basket_w
        continue

    latest_comp = max(comp_entries, key=lambda c: c['rebal_date'])
    tickers     = [t.upper() for t in latest_comp['composition']]

    w_brvm30 = get_total_cap_weights(tickers, dt, sh, soc)
    new_basket_w, exclu_info, forced_tks = build_adv_capped_weights(
        w_brvm30, dt, AUM_MFCFA, sh, old_basket_w
    )

    # Verifier les changements vs anciens poids
    old_w_hist = dd.get('w_history', {}).get(dt, {})
    if isinstance(old_w_hist, list) and len(old_w_hist) == 2:
        old_w_hist = old_w_hist[1]

    changed = []
    for tk in set(old_w_hist) | set(new_basket_w):
        w_old = old_w_hist.get(tk, 0)
        w_new = new_basket_w.get(tk, 0)
        if abs(w_new - w_old) > 0.0001:
            changed.append((tk, w_old * 100, w_new * 100))

    if changed:
        print(f'  Changements de poids ({len(changed)} titres) :')
        for tk, wo, wn in sorted(changed, key=lambda x: -abs(x[2]-x[1])):
            print(f'    {tk:<8} {wo:.2f}% -> {wn:.2f}% (delta {wn-wo:+.2f}%)')
    else:
        print(f'  Aucun changement de poids.')

    print(f'  Panier : {len(new_basket_w)} titres | {len(exclu_info)} exclus')

    # Mise a jour w_history
    w_history_new[dt] = new_basket_w

    # Mise a jour rebal_detail basket
    if dt in rebal_map:
        updated_basket = []
        for tk, w in sorted(new_basket_w.items(), key=lambda x: -x[1]):
            adv_tk = compute_adv(sh, tk, dt)
            w_b30  = w_brvm30.get(tk, 0)
            updated_basket.append({
                'ticker':    tk,
                'w_etf':     round(w, 6),
                'w_brvm30':  round(w_b30, 6),
                'adv_mfcfa': round(adv_tk, 1),
                'force':     tk in forced_tks,
                'force_otc': tk in forced_tks,
            })
        rebal_map[dt]['basket'] = updated_basket

    old_basket_w = dict(new_basket_w)

# ── Sauvegarde ────────────────────────────────────────────────────────────────
print('\n\nSauvegarde...')

# dashboard_data.json : mise a jour w_history
for dt, w in w_history_new.items():
    dd['w_history'][dt] = w
json.dump(dd, open(os.path.join(DATA, 'dashboard_data.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, separators=(',', ':'))
print('  dashboard_data.json -> w_history mis a jour')

# rebal_detail.json
json.dump(rd, open(os.path.join(DATA, 'rebal_detail.json'), 'w', encoding='utf-8'),
          ensure_ascii=False, indent=2)
print('  rebal_detail.json -> baskets mis a jour')
print('\nDone. Lance maintenant : python scripts/compute_te_progressive.py')
