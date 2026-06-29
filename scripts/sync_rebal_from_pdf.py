"""
sync_rebal_from_pdf.py
======================
Synchronise rebal_detail.json depuis les compositions officielles PDF BRVM30
(brvm_composition_history.json) puis relance rebuild_backtest.py.

Pour chaque date de rebalancement ayant une composition PDF validée :
  1. Les 30 tickers officiels BRVM (source de vérité)
  2. Poids estimés depuis les prix Sika à cette date (proxy market-cap)
  3. Mise à jour basket + excluded pour que leur union = 30 tickers officiels
  4. Relance rebuild_backtest.py pour recalculer toutes les métriques

Usage : python scripts/sync_rebal_from_pdf.py
"""
import os, sys, json, subprocess
import pandas as pd
import openpyxl
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')

# ── Chargement ────────────────────────────────────────────────────────────────
print("Chargement des données...")
sh  = json.load(open(os.path.join(DATA, 'sika_history.json'),              encoding='utf-8'))
rd  = json.load(open(os.path.join(DATA, 'rebal_detail.json'),              encoding='utf-8'))
bch = json.load(open(os.path.join(DATA, 'brvm_composition_history.json'), encoding='utf-8'))

# ── Poids officiels BRVM30 depuis l'Excel ────────────────────────────────────
print("Lecture poids BRVM30 depuis Excel...")
EXCEL_PATH = os.path.join(BASE, 'excel', 'BRVM_Consolidated_Kendall_updated.xlsx')
excel_weights = {}   # {rebal_date: {ticker: weight}}

try:
    wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True, data_only=True)
    ws = wb['⚖️ Poids_Matrice_BRVM30']
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    header = rows[0]
    # Dates en format DD/MM/YYYY → convertir en YYYY-MM-DD
    date_cols = {}
    for i, h in enumerate(header):
        if i < 3 or not h:
            continue
        try:
            d = pd.to_datetime(h, dayfirst=True).strftime('%Y-%m-%d')
            date_cols[i] = d
        except Exception:
            pass
    # Remplir excel_weights
    for row in rows[1:]:
        ticker = str(row[0]).strip().upper() if row[0] else None
        if not ticker:
            continue
        for col_i, date_str in date_cols.items():
            val = row[col_i]
            try:
                fval = float(val)
                if fval > 0:
                    excel_weights.setdefault(date_str, {})[ticker] = fval
            except (TypeError, ValueError):
                pass
    print(f"  {len(excel_weights)} dates chargées depuis Excel : {sorted(excel_weights.keys())}")
except Exception as e:
    print(f"  [WARN] Impossible de lire l'Excel : {e} — fallback Sika uniquement")

# ── Index PDF par rebal_date (tolérance ±15 jours) ───────────────────────────
comp_pdf      = [c for c in bch if c.get('rebal_date') and len(c.get('composition', [])) >= 25]
comp_by_date  = {c['rebal_date']: c for c in comp_pdf}

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

def get_weights(tickers, rebal_date):
    """
    Poids BRVM30 officiels depuis Excel en priorité.
    Fallback price-weighted Sika pour les titres absents de l'Excel.
    """
    # Poids Excel pour cette date
    ex_w = excel_weights.get(rebal_date, {})

    # Titres manquants dans l'Excel → prix Sika
    missing = [tk for tk in tickers if tk not in ex_w or ex_w[tk] == 0]
    sika_prices = {}
    for tk in missing:
        p = last_price(tk, rebal_date)
        if p:
            sika_prices[tk] = p

    # Si tous les tickers sont dans l'Excel, normaliser directement
    combined = {tk: ex_w[tk] for tk in tickers if tk in ex_w and ex_w[tk] > 0}

    if sika_prices:
        # Estimation pour les manquants : proportionnel au prix moyen des autres
        avg_w = sum(combined.values()) / len(combined) if combined else 1 / len(tickers)
        sika_total = sum(sika_prices.values())
        for tk, p in sika_prices.items():
            combined[tk] = avg_w * (p / (sika_total / len(sika_prices)))

    # Titres sans aucune donnée → poids moyen
    for tk in tickers:
        if tk not in combined:
            combined[tk] = avg_w if combined else 1 / len(tickers)

    # Normaliser à 1
    total = sum(combined.values())
    return {tk: round(combined[tk] / total, 6) for tk in tickers} if total > 0 else \
           {tk: round(1 / len(tickers), 6) for tk in tickers}

# ── Dictionnaires globaux (secteur + float exclus connus) ────────────────────
secteur_map          = {}
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
print('\n=== Synchronisation rebal_detail ← PDF officiels BRVM ===\n')
n_updated = 0

for r in rd.get('rebalancings', []):
    dt  = r['date']
    pdf = find_pdf_comp(dt)

    if not pdf:
        print(f'{dt}: aucun PDF trouvé — inchangé')
        continue

    official = [t.upper() for t in pdf.get('composition', [])]
    if len(official) < 25:
        print(f'{dt}: composition incomplète ({len(official)} tickers) — inchangé')
        continue

    official_set   = set(official)
    current_basket = {b['ticker'] for b in r.get('basket',   [])}
    current_excl   = {e['ticker'] for e in r.get('excluded', [])}
    current_univ   = current_basket | current_excl

    only_pdf = official_set - current_univ
    only_rd  = current_univ - official_set

    composition_ok = not only_pdf and not only_rd

    if composition_ok:
        print(f'{dt}: composition OK — mise à jour des poids Excel')
    else:
        print(f'{dt}: composition + poids')
        if only_pdf: print(f'  + Ajouter : {sorted(only_pdf)}')
        if only_rd:  print(f'  - Retirer : {sorted(only_rd)}')

    weights = get_weights(official, dt)

    # Index des entrées existantes
    ex_basket = {b['ticker']: b for b in r.get('basket',   [])}
    ex_excl   = {e['ticker']: e for e in r.get('excluded', [])}

    new_basket  = []
    new_excl    = []

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
            # Ticker officiellement dans BRVM30 mais connu pour float insuffisant
            new_excl.append({
                'ticker':      tk,
                'w_brvm30':    w,
                'raison':      'Float < 7 Md FCFA',
                'adv_mfcfa':   adv,
                'stale_ratio': stale,
                'secteur':     sect,
            })
        else:
            # Ticker présent dans PDF mais absent de rebal_detail → basket provisoire
            # rebuild_backtest.py appliquera les règles (ADV, stale, force) ensuite
            new_basket.append({
                'ticker':      tk,
                'w_etf':       w,
                'w_brvm30':    w,
                'force':       w >= 0.03,
                'adv_mfcfa':   adv,
                'stale_ratio': stale,
                'secteur':     sect,
            })

    # Tickers dans rebal_detail mais pas dans PDF → supprimés (they leave the index)
    for tk in only_rd:
        print(f'  Supprime {tk} (absent du PDF officiel)')

    r['basket']   = new_basket
    r['excluded'] = new_excl
    r['basket_n'] = len(new_basket)
    r['excl_n']   = len(new_excl)
    r['coverage'] = round(sum(b.get('w_brvm30', 0) for b in new_basket), 4)
    r['excl_w']   = round(sum(e.get('w_brvm30', 0) for e in new_excl),   4)
    n_updated += 1
    # Source des poids
    ex_w = excel_weights.get(dt, {})
    n_from_excel = sum(1 for tk in official if tk in ex_w and ex_w[tk] > 0)
    n_from_sika  = len(official) - n_from_excel
    print(f'  Poids : {n_from_excel} depuis Excel, {n_from_sika} estimés Sika')

# ── Sauvegarde ────────────────────────────────────────────────────────────────
if n_updated == 0:
    print('\nAucune mise a jour necessaire — compositions et poids déjà synchronisés.')
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
