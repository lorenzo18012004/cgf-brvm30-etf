"""
sync_rebal_from_pdf.py
======================
Synchronise rebal_detail.json depuis les compositions officielles PDF BRVM30
(brvm_composition_history.json) puis relance rebuild_backtest.py.

Pour chaque date de rebalancement ayant une composition PDF validee :
  1. Les 30 tickers officiels BRVM (source de verite) depuis les PDFs
  2. Poids calcules depuis la capitalisation TOTALE Sika :
       w_i = nb_titres_i x prix_i(t)
     (sans flottant : le BRVM30 Sika est pondere par capi totale)
  3. Mise a jour basket + excluded pour que leur union = 30 tickers officiels
  4. Relance rebuild_backtest.py pour recalculer toutes les metriques

Source des donnees :
  - sika_societe.json : nb_titres scrappe depuis sikafinance.com/marches/societe/
  - sika_history.json : prix journaliers Sika
  - brvm_composition_history.json : compositions officielles PDFs BRVM

Usage : python scripts/sync_rebal_from_pdf.py
"""
import os, sys, json, subprocess
import pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')

# ── Chargement ────────────────────────────────────────────────────────────────
print("Chargement des donnees...")
sh  = json.load(open(os.path.join(DATA, 'sika_history.json'),              encoding='utf-8'))
rd  = json.load(open(os.path.join(DATA, 'rebal_detail.json'),              encoding='utf-8'))
bch = json.load(open(os.path.join(DATA, 'brvm_composition_history.json'), encoding='utf-8'))
soc = json.load(open(os.path.join(DATA, 'sika_societe.json'),             encoding='utf-8'))

print(f"  {len(soc)} titres charges depuis sika_societe.json")

# ── Index PDF par rebal_date (tolerance ±15 jours) ───────────────────────────
comp_pdf     = [c for c in bch if c.get('rebal_date') and len(c.get('composition', [])) >= 25]
comp_by_date = {c['rebal_date']: c for c in comp_pdf}

def find_pdf_comp(rebal_date):
    if rebal_date in comp_by_date:
        return comp_by_date[rebal_date]
    dt = pd.Timestamp(rebal_date)
    for delta in range(1, 16):
        for sign in (-1, 1):
            candidate = (dt + pd.Timedelta(days=sign * delta)).strftime('%Y-%m-%d')
            if candidate in comp_by_date:
                return comp_by_date[candidate]
    return None

# ── Helpers Sika ─────────────────────────────────────────────────────────────
def last_price(ticker, as_of_date):
    hist = sh.get(ticker, {})
    past = sorted(d for d in hist if d <= as_of_date)
    if past:
        p = hist[past[-1]].get('close')
        if p and float(p) > 0:
            return float(p)
    return None

def compute_adv(ticker, as_of_date, window=63):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    vals  = [(hist[d].get('volume') or 0) * (hist[d].get('close') or 0) / 1e6
             for d in dates]
    return round(float(sum(vals) / len(dates)), 1) if dates else 0.0

def compute_stale(ticker, as_of_date, window=63):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    if not dates:
        return 1.0
    return round(sum(1 for d in dates if (hist[d].get('volume') or 0) == 0) / len(dates), 3)

# ── Calcul des poids par capitalisation totale Sika ──────────────────────────
def get_weights(tickers, rebal_date):
    """
    Poids BRVM30 par capitalisation TOTALE :
      w_i = nb_titres_i x prix_i(rebal_date)

    Le BRVM30 sur Sika est pondere par capitalisation totale (pas flottant).
    Utiliser le flottant ferait diverger les poids du benchmark Sika.
    Fallback poids egaux si donnees manquantes.
    """
    market_cap = {}
    missing = []
    for tk in tickers:
        nb   = soc.get(tk, {}).get('nb_titres')
        prix = last_price(tk, rebal_date)
        if nb and prix:
            market_cap[tk] = nb * prix
        else:
            missing.append(tk)

    if missing:
        avg_mc = (sum(market_cap.values()) / len(market_cap)) if market_cap else 1.0
        for tk in missing:
            market_cap[tk] = avg_mc

    total = sum(market_cap.values())
    if total <= 0:
        return {tk: round(1 / len(tickers), 6) for tk in tickers}
    return {tk: round(market_cap[tk] / total, 6) for tk in tickers}

# ── Dictionnaires globaux (secteur + float exclus connus) ────────────────────
secteur_map           = {}
float_excluded_global = set()

for r in rd.get('rebalancings', []):
    for item in r.get('basket', []) + r.get('excluded', []):
        tk = item.get('ticker', '')
        s  = item.get('secteur', '—')
        if tk and s and s != '—':
            secteur_map[tk] = s
        if 'Float' in str(item.get('raison', '')):
            float_excluded_global.add(tk)

# ── Synchronisation ───────────────────────────────────────────────────────────
print('\n=== Synchronisation rebal_detail <- PDF officiels BRVM ===\n')
n_updated = 0

for r in rd.get('rebalancings', []):
    dt  = r['date']
    pdf = find_pdf_comp(dt)

    if not pdf:
        print(f'{dt}: aucun PDF trouve - inchange')
        continue

    official = [t.upper() for t in pdf.get('composition', [])]
    if len(official) < 25:
        print(f'{dt}: composition incomplete ({len(official)} tickers) - inchange')
        continue

    official_set   = set(official)
    current_basket = {b['ticker'] for b in r.get('basket',   [])}
    current_excl   = {e['ticker'] for e in r.get('excluded', [])}
    current_univ   = current_basket | current_excl

    only_pdf = official_set - current_univ
    only_rd  = current_univ - official_set

    composition_ok = not only_pdf and not only_rd

    if composition_ok:
        print(f'{dt}: composition OK - mise a jour des poids capi totale Sika')
    else:
        print(f'{dt}: composition + poids')
        if only_pdf: print(f'  + Ajouter : {sorted(only_pdf)}')
        if only_rd:  print(f'  - Retirer : {sorted(only_rd)}')

    weights = get_weights(official, dt)

    n_sika = sum(1 for tk in official if soc.get(tk, {}).get('nb_titres') and last_price(tk, dt))
    n_miss = len(official) - n_sika
    print(f'  Poids : {n_sika} via capi totale Sika, {n_miss} fallback')

    ex_basket = {b['ticker']: b for b in r.get('basket',   [])}
    ex_excl   = {e['ticker']: e for e in r.get('excluded', [])}

    new_basket = []
    new_excl   = []

    for tk in official:
        w     = weights[tk]
        adv   = compute_adv(tk, dt)
        stale = compute_stale(tk, dt)
        sect  = secteur_map.get(tk, '—')

        if tk in ex_basket:
            entry = dict(ex_basket[tk])
            entry.update({'w_brvm30': w, 'adv_mfcfa': adv, 'stale_ratio': stale})
            new_basket.append(entry)
        elif tk in ex_excl:
            entry = dict(ex_excl[tk])
            entry.update({'w_brvm30': w, 'adv_mfcfa': adv, 'stale_ratio': stale})
            new_excl.append(entry)
        elif tk in float_excluded_global:
            new_excl.append({
                'ticker':      tk,
                'w_brvm30':    w,
                'raison':      'Float < 7 Md FCFA',
                'adv_mfcfa':   adv,
                'stale_ratio': stale,
                'secteur':     sect,
            })
        else:
            new_basket.append({
                'ticker':      tk,
                'w_etf':       w,
                'w_brvm30':    w,
                'force':       w >= 0.03,
                'adv_mfcfa':   adv,
                'stale_ratio': stale,
                'secteur':     sect,
            })

    for tk in only_rd:
        print(f'  Supprime {tk} (absent du PDF officiel)')

    r['basket']   = new_basket
    r['excluded'] = new_excl
    r['basket_n'] = len(new_basket)
    r['excl_n']   = len(new_excl)
    r['coverage'] = round(sum(b.get('w_brvm30', 0) for b in new_basket), 4)
    r['excl_w']   = round(sum(e.get('w_brvm30', 0) for e in new_excl),   4)
    n_updated += 1

# ── Sauvegarde ────────────────────────────────────────────────────────────────
if n_updated == 0:
    print('\nAucune mise a jour necessaire.')
    sys.exit(0)

rd_path = os.path.join(DATA, 'rebal_detail.json')
json.dump(rd, open(rd_path, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print(f'\n{n_updated} rebalancement(s) mis a jour dans rebal_detail.json')

# ── Rebuild backtest ──────────────────────────────────────────────────────────
print('\nRelance rebuild_backtest.py...\n')
result = subprocess.run(
    [sys.executable, os.path.join(BASE, 'scripts', 'rebuild_backtest.py')],
    capture_output=False, text=True,
)
if result.returncode != 0:
    print('[ERREUR] rebuild_backtest.py a echoue')
    sys.exit(1)

print('\nSynchronisation terminee.')
