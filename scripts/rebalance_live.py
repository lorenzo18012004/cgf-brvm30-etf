"""
rebalance_live.py
=================
Rebalancement trimestriel automatique du panier live (nav_latest.json).

Détecte si aujourd'hui est le premier jour ouvré d'un trimestre BRVM30
(janvier / avril / juillet / octobre) et applique les nouveaux poids.

Méthode identique au backtest :
  - Poids cible     : capitalisation totale Sika (nb_titres × prix)
  - Contrainte ADV  : grands titres (>=3%) plafonnés à ADV × 62j / AUM
                      petits titres (< 3%) plafonnés à ADV × 32j / AUM
  - Excès redistribué proportionnellement aux non-plafonnés
  - Titres sans ADV mesurable ou poids < 0.1% : exclus

Sorties :
  - data/nav_latest.json     mis à jour (nouveau basket + coûts de tx appliqués)
  - data/dashboard_data.json mis à jour (w_history + rebal_dates)
  - data/rebal_detail.json   mis à jour (nouvelle entrée de rebalancement)
  - stdout                   ordres d'achat / vente

Usage :
  python scripts/rebalance_live.py              # auto-détecte + dry-run si c'est le jour J
  python scripts/rebalance_live.py --dry-run    # calcule les ordres sans rien écrire (défaut en CI)
  python scripts/rebalance_live.py --apply      # applique vraiment le rebalancement
  python scripts/rebalance_live.py --force      # force même si ce n'est pas le jour J
  python scripts/rebalance_live.py --date 2026-07-01  # simule à une date précise

Sécurité :
  Sans --apply, le script ne modifie AUCUN fichier.
  Le workflow quotidien tourne toujours en dry-run (prévisualisation).
  Pour appliquer, déclencher manuellement le workflow "Appliquer rebalancement"
  depuis GitHub Actions → onglet Actions → "Appliquer rebalancement trimestriel".
"""
import sys, os, json, argparse
import pandas as pd
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')

NL_PATH  = os.path.join(DATA, 'nav_latest.json')
SH_PATH  = os.path.join(DATA, 'sika_history.json')
SOC_PATH = os.path.join(DATA, 'sika_societe.json')
BCH_PATH = os.path.join(DATA, 'brvm_composition_history.json')
DD_PATH  = os.path.join(DATA, 'dashboard_data.json')
RD_PATH  = os.path.join(DATA, 'rebal_detail.json')

# ── Paramètres (identiques à rebuild_backtest.py) ─────────────────────────────
MAX_EXEC_LARGE   = 62      # jours pour grands titres (>= 3% BRVM30)
MAX_EXEC_SMALL   = 32      # jours pour petits titres (< 3% BRVM30)
LARGE_THRESHOLD  = 0.03
PARTICIPATION_RATE = 0.20  # max 20% de l'ADV quotidien (screen + OTC petits blocs)
MIN_ADV_MFCFA    = 0.5
MIN_WEIGHT       = 0.001
STALE_WINDOW     = 63
FORCE_TOP_N      = 5       # top 5 titres BRVM30 tenus à leur poids exact (OTC)
CASH_BUFFER      = 0.01   # poche de liquidité : 1% du NAV en cash


def spread_one_way(adv_mfcfa: float) -> float:
    """Spread bid-ask one-way selon la liquidité du titre."""
    if adv_mfcfa >= 100: return 0.0025
    if adv_mfcfa >=  30: return 0.0040
    if adv_mfcfa >=  10: return 0.0080
    if adv_mfcfa >=   5: return 0.0125
    return 0.0175

# ── Détection date de rebalancement ──────────────────────────────────────────
REBAL_MONTHS = {1, 4, 7, 10}   # trimestres BRVM30

def is_rebal_day(date_str: str, tolerance_days: int = 3) -> bool:
    """
    Retourne True si date_str est le 1er jour ouvré d'un mois de rebalancement,
    avec une tolérance de ±tolerance_days jours (au cas où le script tourne en retard).
    """
    dt = pd.Timestamp(date_str)
    if dt.month not in REBAL_MONTHS:
        return False
    # Premier jour du mois, cherche le 1er jour ouvré
    first = pd.Timestamp(dt.year, dt.month, 1)
    first_bday = first + pd.offsets.BDay(0)
    delta = abs((dt - first_bday).days)
    return delta <= tolerance_days

def next_rebal_date(from_date: str) -> str:
    dt = pd.Timestamp(from_date)
    for months_ahead in range(1, 5):
        candidate_month = ((dt.month - 1 + months_ahead) % 12) + 1
        candidate_year  = dt.year + ((dt.month - 1 + months_ahead) // 12)
        if candidate_month in REBAL_MONTHS:
            first = pd.Timestamp(candidate_year, candidate_month, 1)
            return (first + pd.offsets.BDay(0)).strftime('%Y-%m-%d')
    return ''

# ── Helpers Sika ─────────────────────────────────────────────────────────────
def last_price(sh, ticker, as_of_date):
    hist = sh.get(ticker, {})
    past = sorted(d for d in hist if d <= as_of_date)
    if past:
        p = hist[past[-1]]
        close = p.get('close') if isinstance(p, dict) else p
        if close and float(close) > 0:
            return float(close)
    return None

def compute_adv(sh, ticker, as_of_date, window=STALE_WINDOW):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    vals  = [(hist[d].get('volume') or 0) * (hist[d].get('close') or 0) / 1e6
             for d in dates]
    return float(sum(vals) / len(dates)) if dates else 0.0

def compute_stale(sh, ticker, as_of_date, window=STALE_WINDOW):
    hist  = sh.get(ticker, {})
    dates = sorted(d for d in hist if d < as_of_date)[-window:]
    if not dates:
        return 1.0
    return sum(1 for d in dates if (hist[d].get('volume') or 0) == 0) / len(dates)

# ── Calcul des poids total cap Sika ──────────────────────────────────────────
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

# ── Stratégie hybride : top N forcés OTC + ADV-cap sur les restants ──────────
def build_adv_capped_weights(w_brvm30, rebal_date, aum_mfcfa, sh):
    """
    Top FORCE_TOP_N titres : tenus à leur poids BRVM30 exact (OTC, sans contrainte ADV).
    Restants : ADV-cap 20% × max_days + redistribution classique.
    Retourne (final_weights, exclu_info, forced_set).
    """
    total_brvm30 = sum(w_brvm30.values()) or 1.0
    w_norm = {tk: v / total_brvm30 for tk, v in w_brvm30.items()}
    adv    = {tk: compute_adv(sh, tk, rebal_date) for tk in w_norm}

    # Top N forcés (OTC)
    sorted_tks  = sorted(w_norm, key=lambda x: -w_norm[x])
    forced_tks  = set(sorted_tks[:FORCE_TOP_N])
    rest_tks    = [tk for tk in sorted_tks if tk not in forced_tks]

    forced_w     = {tk: w_norm[tk] for tk in forced_tks}
    forced_total = sum(forced_w.values())
    rest_budget  = 1.0 - forced_total

    # Restants : filtrage ADV
    eligible = [tk for tk in rest_tks if adv[tk] >= MIN_ADV_MFCFA]
    exclu    = [tk for tk in rest_tks if adv[tk] < MIN_ADV_MFCFA]

    if not eligible:
        return {tk: round(v, 6) for tk, v in forced_w.items()}, {tk: 'ADV insuffisant' for tk in exclu}, forced_tks

    total_rest = sum(w_norm[tk] for tk in eligible) or 1.0
    weights = {tk: w_norm[tk] / total_rest * rest_budget for tk in eligible}

    max_w = {}
    for tk in eligible:
        days = MAX_EXEC_LARGE if w_norm[tk] >= LARGE_THRESHOLD else MAX_EXEC_SMALL
        max_w[tk] = min(PARTICIPATION_RATE * adv[tk] * days / aum_mfcfa, rest_budget)

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

    for _ in range(10):
        tiny = [tk for tk in eligible if 0 < weights[tk] < MIN_WEIGHT]
        if not tiny:
            break
        for tk in tiny:
            exclu.append(tk)
            eligible.remove(tk)
        if not eligible:
            break
        total_keep = sum(weights[tk] for tk in eligible)
        for tk in eligible:
            weights[tk] = weights[tk] / total_keep * rest_budget if total_keep > 0 else rest_budget / len(eligible)

    final = {**forced_w, **{tk: weights[tk] for tk in eligible if weights.get(tk, 0) > 0}}
    total = sum(final.values())
    if total > 0:
        final = {tk: round(v / total, 6) for tk, v in final.items()}

    exclu_info = {}
    for tk in exclu:
        if adv.get(tk, 0) < MIN_ADV_MFCFA:
            exclu_info[tk] = f'ADV {adv.get(tk,0):.1f} MFCFA < {MIN_ADV_MFCFA}'
        else:
            exclu_info[tk] = f'Poids < {MIN_WEIGHT*100:.1f}%'

    return final, exclu_info, forced_tks


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply',    action='store_true',
                        help='Applique vraiment le rebalancement (modifie les fichiers)')
    parser.add_argument('--force',    action='store_true',
                        help='Ignore la vérification de date')
    parser.add_argument('--dry-run',  action='store_true', dest='dry_run',
                        help='Calcule les ordres sans rien écrire (comportement par défaut)')
    parser.add_argument('--date',     default=None,
                        help='Date du rebalancement (YYYY-MM-DD), défaut = aujourd\'hui')
    args = parser.parse_args()

    # Sans --apply, on est toujours en dry-run
    dry_run = not args.apply

    today = args.date or pd.Timestamp.now().strftime('%Y-%m-%d')

    # ── Vérification date ──────────────────────────────────────────────────────
    if not args.force and not is_rebal_day(today):
        print(f"[SKIP] {today} n'est pas une date de rebalancement BRVM30.")
        print(f"       Prochain rebal estimé : {next_rebal_date(today)}")
        print("       Pour simuler : --force   Pour appliquer : --force --apply")
        sys.exit(0)

    mode = "DRY-RUN (prévisualisation)" if dry_run else "APPLICATION RÉELLE"
    print(f"=== REBALANCEMENT BRVM30 ETF — {today} [{mode}] ===")
    print()

    # ── Chargement ────────────────────────────────────────────────────────────
    sh  = json.load(open(SH_PATH,  encoding='utf-8'))
    soc = json.load(open(SOC_PATH, encoding='utf-8'))
    nl  = json.load(open(NL_PATH,  encoding='utf-8'))
    dd  = json.load(open(DD_PATH,  encoding='utf-8'))
    rd  = json.load(open(RD_PATH,  encoding='utf-8'))
    bch = json.load(open(BCH_PATH, encoding='utf-8'))

    aum_mfcfa = float(nl.get('aum_mfcfa', 5000))
    last_rebal = nl.get('last_rebal_date', '2026-04-01')

    # Vérifier qu'on ne rebalance pas deux fois la même date (sauf dry-run)
    if today <= last_rebal and not args.force and not dry_run:
        print(f"[SKIP] Dernier rebal : {last_rebal} — déjà à jour.")
        sys.exit(0)

    # ── Composition BRVM30 ────────────────────────────────────────────────────
    # Prendre la dernière composition connue avant ou égale à today
    comp_entries = [c for c in bch if c.get('rebal_date') and
                    len(c.get('composition', [])) >= 25 and
                    c['rebal_date'] <= today]

    if comp_entries:
        latest_comp = max(comp_entries, key=lambda c: c['rebal_date'])
        tickers = [t.upper() for t in latest_comp['composition']]
        print(f"[1/5] Composition : {len(tickers)} titres (PDF du {latest_comp['rebal_date']})")
    else:
        # Fallback : composition du dernier rebal en portefeuille
        tickers = [item['ticker'] for item in nl.get('basket', [])]
        print(f"[1/5] Composition : {len(tickers)} titres (basket actuel — pas de PDF récent)")

    # ── Poids total cap Sika ──────────────────────────────────────────────────
    print("[2/5] Calcul des poids total cap Sika…")
    w_brvm30 = get_total_cap_weights(tickers, today, sh, soc)

    # ── Stratégie hybride OTC + ADV-cap ──────────────────────────────────────
    print(f"[3/5] Top {FORCE_TOP_N} OTC + ADV-cap 20%×62j/32j sur les restants…")
    new_basket_w, exclu_info, forced_tks = build_adv_capped_weights(w_brvm30, today, aum_mfcfa, sh)

    sorted_by_w = sorted(w_brvm30, key=lambda x: -w_brvm30.get(x, 0))
    print(f"   Top {FORCE_TOP_N} OTC : {', '.join(sorted_by_w[:FORCE_TOP_N])}")
    print(f"   Panier final : {len(new_basket_w)} titres | {len(exclu_info)} exclus")
    for tk, raison in sorted(exclu_info.items()):
        print(f"   EXCLU {tk} : {raison}")

    # ── Ordres d'achat / vente ────────────────────────────────────────────────
    print()
    print("[4/5] Ordres de rebalancement…")
    old_basket = {item['ticker']: item['poids_pct'] / 100
                  for item in nl.get('basket', [])}

    all_tickers = sorted(set(old_basket) | set(new_basket_w))
    orders = []
    turnover = 0.0

    for tk in all_tickers:
        w_old = old_basket.get(tk, 0.0)
        w_new = new_basket_w.get(tk, 0.0)
        delta = w_new - w_old
        if abs(delta) < 0.0001:
            continue
        montant_mfcfa = abs(delta) * aum_mfcfa
        sens = 'ACHETER' if delta > 0 else 'VENDRE'
        orders.append({
            'ticker': tk, 'sens': sens,
            'delta_pct': round(delta * 100, 2),
            'montant_mfcfa': round(montant_mfcfa, 1),
            'w_old_pct': round(w_old * 100, 2),
            'w_new_pct': round(w_new * 100, 2),
        })
        turnover += abs(delta)

    turnover /= 2   # one-way turnover
    # Coût variable : spread selon ADV de chaque titre
    cost_pct = sum(
        abs(new_basket_w.get(tk, 0) - old_basket.get(tk, 0)) *
        spread_one_way(compute_adv(sh, tk, today))
        for tk in all_tickers
    ) / 2
    cash_mfcfa = aum_mfcfa * CASH_BUFFER
    print(f"   Poche de liquidité : {CASH_BUFFER*100:.0f}% = {cash_mfcfa:.0f} MFCFA en cash")
    print(f"   AUM investi (panier) : {(1-CASH_BUFFER)*100:.0f}% = {aum_mfcfa*(1-CASH_BUFFER):.0f} MFCFA")
    print(f"   Turnover one-way : {turnover*100:.1f}%")
    print(f"   Coût de transaction (spread variable) : {cost_pct*100:.3f}% de l'AUM")
    print()
    print(f"   {'Ticker':<8} {'Sens':<8} {'Delta':>8} {'Montant':>12} {'Ancien':>8} {'Nouveau':>8}")
    print(f"   {'-'*60}")
    for o in sorted(orders, key=lambda x: -abs(x['delta_pct'])):
        print(f"   {o['ticker']:<8} {o['sens']:<8} {o['delta_pct']:>+7.2f}%"
              f" {o['montant_mfcfa']:>10.1f} MFCFA"
              f" {o['w_old_pct']:>7.2f}% → {o['w_new_pct']:.2f}%")

    # ── Calcul NAV après coûts ────────────────────────────────────────────────
    nav_before = float(nl.get('nav_indice', 0))
    nav_after  = nav_before * (1 - cost_pct)

    # ── Résumé dry-run ────────────────────────────────────────────────────────
    if dry_run:
        print()
        print("=" * 60)
        print("  [DRY-RUN] AUCUN FICHIER MODIFIÉ")
        print(f"  Panier projeté   : {len(new_basket_w)} titres")
        print(f"  Turnover one-way : {turnover*100:.1f}%")
        print(f"  Coût transaction : {cost_pct*100:.3f}%")
        print(f"  NAV projetée     : {nav_before:.4f} → {nav_after:.4f}")
        print()
        print("  Pour appliquer le rebalancement :")
        print("  → GitHub Actions > 'Appliquer rebalancement trimestriel' > Run workflow")
        print("  → ou : python scripts/rebalance_live.py --apply [--force]")
        print("=" * 60)
        return

    # ── Application réelle : écriture des fichiers ────────────────────────────
    print()
    print("[5/5] Application du rebalancement…")

    new_basket = []
    for tk, w in sorted(new_basket_w.items(), key=lambda x: -x[1]):
        prix  = last_price(sh, tk, today)
        stale = compute_stale(sh, tk, today) > 0.5
        adv   = compute_adv(sh, tk, today)
        new_basket.append({
            'ticker':       tk,
            'poids_pct':    round(w * 100, 4),
            'pv_mfcfa':     round(w * aum_mfcfa * (1 - CASH_BUFFER) * (1 - cost_pct), 1),
            'dernier_prix': prix,
            'prix_stale':   stale,
            'adv_mfcfa':    round(adv, 1),
            'w_brvm30':     round(w_brvm30.get(tk, 0), 6),
            'force_otc':    tk in forced_tks,
        })

    nl['basket']           = new_basket
    nl['n_basket']         = len(new_basket)
    nl['last_rebal_date']  = today
    nl['nav_indice']       = round(nav_after, 4)
    nl['aum_mfcfa']        = round(nav_after / nav_before * aum_mfcfa, 1) if nav_before > 0 else aum_mfcfa
    nl['cash_buffer_pct']  = CASH_BUFFER * 100

    json.dump(nl, open(NL_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f"   nav_latest.json    : {len(new_basket)} titres | NAV {nav_before:.4f} → {nav_after:.4f}")

    dd.setdefault('w_history', {})[today] = new_basket_w
    json.dump(dd, open(DD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print(f"   dashboard_data.json: w_history[{today}] ajouté")

    new_rebal_entry = {
        'date':     today,
        'skipped':  False,
        'basket_n': len(new_basket),
        'excl_n':   len(exclu_info),
        'coverage': round(sum(w_brvm30.get(tk, 0) for tk in new_basket_w), 4),
        'excl_w':   round(sum(w_brvm30.get(tk, 0) for tk in exclu_info), 4),
        'turnover': round(turnover, 4),
        'cost_tx':  round(cost_pct, 6),
        'basket': [
            {
                'ticker':      tk,
                'w_etf':       round(w, 6),
                'w_brvm30':    round(w_brvm30.get(tk, 0), 6),
                'adv_mfcfa':   round(compute_adv(sh, tk, today), 1),
                'stale_ratio': round(compute_stale(sh, tk, today), 3),
                'force':       tk in forced_tks,
                'force_otc':   tk in forced_tks,
            }
            for tk, w in new_basket_w.items()
        ],
        'excluded': [
            {
                'ticker':      tk,
                'w_brvm30':    round(w_brvm30.get(tk, 0), 6),
                'raison':      raison,
                'adv_mfcfa':   round(compute_adv(sh, tk, today), 1),
                'stale_ratio': round(compute_stale(sh, tk, today), 3),
            }
            for tk, raison in exclu_info.items()
        ],
        'orders': orders,
    }

    rebals = [r for r in rd.get('rebalancings', []) if r.get('date') != today]
    rebals.append(new_rebal_entry)
    rebals.sort(key=lambda r: r['date'])
    rd['rebalancings'] = rebals
    json.dump(rd, open(RD_PATH, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print(f"   rebal_detail.json  : entrée {today} ajoutée")

    print()
    print("=== REBALANCEMENT APPLIQUÉ ===")
    print(f"   Panier : {len(new_basket)} titres")
    print(f"   Turnover one-way : {turnover*100:.1f}%")
    print(f"   Coût de transaction : {cost_pct*100:.3f}%")
    print(f"   NAV après coûts : {nav_after:.4f}")


if __name__ == '__main__':
    main()
