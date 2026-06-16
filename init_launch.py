"""
init_launch.py — Initialise le lancement officiel du CGF BRVM30 ETF
====================================================================
À lancer UNE SEULE FOIS le jour du lancement.
Fixe la date, la VL de référence et la composition du panier.
Crée launch_state.json qui sert d'ancre pour tous les calculs live.

Usage :
    python init_launch.py              # lancement aujourd'hui
    python init_launch.py --date 2026-05-26   # forcer une date
    python init_launch.py --par 100000        # valeur faciale (défaut 100 000 FCFA)
"""

import sys, io, os, json, argparse, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
warnings.filterwarnings('ignore')

import pandas as pd

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
LAUNCH_FILE    = os.path.join(BASE_DIR, 'launch_state.json')
NAV_LATEST     = os.path.join(BASE_DIR, 'nav_latest.json')
DEFAULT_PAR    = 100_000    # FCFA par part
DEFAULT_AUM    = 5_000_000_000  # AUM initial de référence (5 Mds FCFA)


def init_launch(launch_date: str, par_fcfa: float, aum_fcfa: float) -> dict:
    os.chdir(BASE_DIR)

    # Vérifier que nav_latest.json est à jour
    if not os.path.exists(NAV_LATEST):
        print("[ERREUR] nav_latest.json introuvable. Lancez d'abord : python calc_nav.py")
        sys.exit(1)

    with open(NAV_LATEST, encoding='utf-8') as f:
        nl = json.load(f)

    # VL backtest au jour du lancement = ancre de référence
    nav_at_launch   = nl['nav_indice']      # indice (ex: 230.45)
    calc_date       = nl['calc_date']       # dernier prix disponible

    n_parts = int(aum_fcfa / par_fcfa)
    aum_at_launch = n_parts * par_fcfa

    state = {
        'launch_date':          launch_date,
        'par_fcfa':             int(par_fcfa),
        'n_parts':              n_parts,
        'aum_initial_fcfa':     aum_at_launch,
        'aum_initial_mfcfa':    round(aum_at_launch / 1_000_000, 1),
        'nav_index_at_launch':  round(nav_at_launch, 6),
        'vl_par_part_initial':  int(par_fcfa),
        'last_prices_date':     calc_date,
        'basket':               nl.get('basket', []),
        'created_at':           pd.Timestamp.now().strftime('%Y-%m-%d %H:%M'),
    }

    with open(LAUNCH_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    # Affichage
    W = 65
    print("=" * W)
    print("  CGF BRVM30 ETF — LANCEMENT OFFICIEL")
    print("=" * W)
    print(f"  Date de lancement       : {launch_date}")
    print(f"  Prix d'emission / part  : {par_fcfa:>12,.0f} FCFA")
    print(f"  Nombre de parts         : {n_parts:>12,}")
    print(f"  AUM initial             : {aum_at_launch/1e9:>12.1f} Mds FCFA")
    print(f"  Ancre backtest (indice) : {nav_at_launch:>12.4f}")
    print()
    print(f"  Formule VL live :")
    print(f"    VL(t) = {par_fcfa:,.0f} x (NAV_index(t) / {nav_at_launch:.4f})")
    print()
    print(f"  Fichier cree : launch_state.json")
    print("=" * W)
    print()
    print("  Prochaines etapes :")
    print("  1. python calc_nav.py         → recalcule la VL avec le lancement")
    print("  2. streamlit run cgf_dashboard.py → voir le dashboard mis a jour")

    return state


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Initialise le lancement CGF BRVM30 ETF')
    parser.add_argument('--date', type=str,
                        default=pd.Timestamp.today().strftime('%Y-%m-%d'),
                        help='Date de lancement (YYYY-MM-DD)')
    parser.add_argument('--par',  type=float, default=DEFAULT_PAR,
                        help=f'Valeur faciale par part FCFA (défaut: {DEFAULT_PAR:,.0f})')
    parser.add_argument('--aum',  type=float, default=DEFAULT_AUM,
                        help=f'AUM initial FCFA (défaut: {DEFAULT_AUM:,.0f})')
    args = parser.parse_args()

    init_launch(args.date, args.par, args.aum)
