"""
dividend_tracker.py — Suivi des dividendes reçus et calcul de la distribution annuelle
========================================================================================
Règles de gestion :
  - Distribution unique le 30 septembre de chaque année
  - Entre réception et distribution, le cash est placé au taux sans risque UEMOA
  - Seuls les dividendes des titres EN PORTEFEUILLE à la date de détachement sont comptés
  - Taux sans risque : BCEAO 7 jours (paramètre configurable)

Produit : dividend_log.json
Format :
{
  "annee": 2026,
  "distribution_date": "2026-06-30",
  "taux_rf_annuel": 0.055,
  "updated_at": "...",
  "evenements": [
    {
      "ticker": "ORAC",
      "date_detach": "2026-06-05",
      "montant_par_action": 800.0,
      "nb_actions_etf": 1562.5,
      "cash_brut_fcfa": 1250000.0,
      "jours_placement": 25,
      "interets_fcfa": 4726.0,
      "cash_total_fcfa": 1254726.0
    },
    ...
  ],
  "total_cash_fcfa": 4500000.0,
  "dividende_par_part_fcfa": 90.0,
  "n_parts": 50000,
  "distribue": false
}

Usage :
    python dividend_tracker.py            # calcule sans distribuer
    python dividend_tracker.py --distribue  # enregistre la distribution (30 juin)
"""

import sys, io, os, json, argparse, warnings
from datetime import date, datetime, timedelta
warnings.filterwarnings('ignore')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DIVS_FILE      = os.path.join(BASE_DIR, 'sika_dividendes.json')
NAV_FILE       = os.path.join(BASE_DIR, 'nav_latest.json')
LAUNCH_FILE    = os.path.join(BASE_DIR, 'launch_state.json')
OUT_FILE       = os.path.join(BASE_DIR, 'dividend_log.json')
RF_CFG_FILE    = os.path.join(BASE_DIR, 'taux_rf_config.json')

_RF_DEFAULT    = 0.055          # fallback si fichier absent
DISTRIBUTION_MM_DD = (9, 30)    # 30 septembre


def _load_taux_rf() -> float:
    if os.path.exists(RF_CFG_FILE):
        try:
            with open(RF_CFG_FILE, encoding='utf-8') as f:
                return float(json.load(f).get('taux_rf_annuel', _RF_DEFAULT))
        except Exception:
            pass
    return _RF_DEFAULT


def _load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _days_to_distrib(detach_date: str, year: int) -> int:
    """Nombre de jours entre la date de paiement et le 30 septembre."""
    distrib = date(year, *DISTRIBUTION_MM_DD)
    try:
        d = date.fromisoformat(detach_date)
    except Exception:
        return 0
    delta = (distrib - d).days
    return max(delta, 0)


def compute(year: int | None = None, distribue: bool = False) -> dict:
    today = date.today()
    if year is None:
        year = today.year
    distrib_date = date(year, *DISTRIBUTION_MM_DD)
    distrib_str  = distrib_date.isoformat()

    divs_data = _load(DIVS_FILE)
    nav_data  = _load(NAV_FILE)
    launch    = _load(LAUNCH_FILE)

    if not divs_data:
        print(f"[WARN] {DIVS_FILE} absent — lance scrape_sika_dividendes.py d'abord")
        return {}
    if not nav_data:
        print(f"[WARN] {NAV_FILE} absent — lance calc_nav.py d'abord")
        return {}

    n_parts  = int((launch or nav_data).get('n_parts', 0))
    TAUX_RF  = _load_taux_rf()
    aum_fcfa = nav_data.get('aum_mfcfa', 0) * 1_000_000
    basket   = {b['ticker']: b for b in nav_data.get('basket', [])}

    launch_date_str = (launch or {}).get('launch_date', '1900-01-01')

    evenements = []
    total_cash = 0.0

    for div in divs_data.get('dividendes', []):
        ticker      = div.get('ticker')
        date_detach = div.get('date_detach')   # None si "À préciser"
        montant     = div.get('montant', 0)

        # Ignorer si ticker inconnu ou pas en portefeuille
        if not ticker or ticker not in basket:
            continue

        # Ignorer si date non connue ou antérieure au lancement
        if not date_detach:
            continue
        if date_detach <= launch_date_str:
            continue

        # Ignorer si la date de détachement est dans le futur (pas encore reçu)
        if date_detach > today.isoformat():
            continue

        # Date de paiement effectif = ex-date + 30 jours (convention BRVM)
        date_paiement = (date.fromisoformat(date_detach) + timedelta(days=30)).isoformat()

        # Nombre d'actions ETF pour ce titre
        w_pct     = basket[ticker].get('poids_pct', 0) / 100.0
        px        = basket[ticker].get('dernier_prix') or 1
        nb_titres = (w_pct * aum_fcfa) / px if px > 0 else 0

        # Cash brut reçu
        cash_brut = nb_titres * montant

        # Intérêts au taux sans risque depuis la date de paiement jusqu'au 30 septembre
        jours = _days_to_distrib(date_paiement, year)
        interets = cash_brut * TAUX_RF * (jours / 365.0)
        cash_total = cash_brut + interets

        total_cash += cash_total
        evenements.append({
            "ticker":               ticker,
            "nom_sika":             div.get('nom_sika', ''),
            "date_detach":          date_detach,
            "date_paiement":        date_paiement,
            "montant_par_action":   round(montant, 2),
            "nb_actions_etf":       round(nb_titres, 2),
            "cash_brut_fcfa":       round(cash_brut, 0),
            "jours_placement":      jours,
            "interets_fcfa":        round(interets, 0),
            "cash_total_fcfa":      round(cash_total, 0),
        })

    div_par_part = (total_cash / n_parts) if n_parts > 0 else 0.0

    result = {
        "annee":                  year,
        "distribution_date":      distrib_str,
        "taux_rf_annuel":         TAUX_RF,
        "updated_at":             datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        "evenements":             sorted(evenements, key=lambda x: x['date_detach']),
        "total_cash_fcfa":        round(total_cash, 0),
        "dividende_par_part_fcfa": round(div_par_part, 2),
        "n_parts":                n_parts,
        "rendement_distribution": round(div_par_part / nav_data.get('vl_par_part_fcfa', 100000) * 100, 3) if nav_data.get('vl_par_part_fcfa') else None,
        "distribue":              distribue and today >= distrib_date,
    }

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ── Affichage terminal ────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  CGF BRVM30 ETF — Suivi dividendes {year}")
    print(f"{'='*60}")
    print(f"  Date de distribution : {distrib_str}")
    print(f"  Taux sans risque     : {TAUX_RF*100:.1f}%")
    print()
    if evenements:
        print(f"  {'Ticker':<8} {'Ex-date':<12} {'Paiement':<12} {'Mont./action':>12} {'Cash ETF':>14} {'Intérêts':>10}")
        print(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*12} {'-'*14} {'-'*10}")
        for e in evenements:
            print(f"  {e['ticker']:<8} {e['date_detach']:<12} "
                  f"{e['date_paiement']:<12} "
                  f"{e['montant_par_action']:>11,.0f}F "
                  f"{e['cash_brut_fcfa']:>13,.0f}F "
                  f"{e['interets_fcfa']:>9,.0f}F")
        print()
        print(f"  Cash total reçu      : {total_cash:>14,.0f} FCFA")
        print(f"  Dividende / part     : {div_par_part:>14,.2f} FCFA")
        if result['rendement_distribution']:
            print(f"  Rendement distribué  : {result['rendement_distribution']:.2f}%")
        print(f"  Statut               : {'DISTRIBUÉ ✓' if result['distribue'] else 'En attente (30 septembre)'}")
    else:
        print("  Aucun dividende reçu pour les titres du panier jusqu'à aujourd'hui.")
    print(f"{'='*60}")
    print(f"  → {OUT_FILE}")

    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Suivi dividendes CGF BRVM30 ETF')
    parser.add_argument('--year',      type=int,  default=None)
    parser.add_argument('--distribue', action='store_true', help='Enregistrer la distribution (30 septembre)')
    args = parser.parse_args()
    compute(year=args.year, distribue=args.distribue)
