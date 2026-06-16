"""
calc_nav_cloud.py -- Fallback NAV quotidien (GitHub Actions, sans Excel)
=======================================================================
Verifie si nav_latest.json est deja a jour pour aujourd hui.
Si non, calcule la VL depuis sika_history.json via calc_nav.py.

Fichiers requis dans le repo :
  sika_history.json   (historique des cours - source principale)
  rebal_detail.json   (composition/poids du panier)
  nav_latest.json     (base J-1 + basket)
  launch_state.json   (optionnel - VL par part)
"""
import sys, os, json, warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
NAV_PATH  = os.path.join(BASE_DIR, "nav_latest.json")


def run() -> None:
    os.chdir(BASE_DIR)
    sys.path.insert(0, BASE_DIR)

    now_utc   = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    if os.path.exists(NAV_PATH):
        with open(NAV_PATH, encoding="utf-8") as f:
            nl = json.load(f)
        last_date = nl.get("calc_date") or nl.get("date", "")
        if last_date >= today_str:
            print(f"[OK] nav_latest.json deja a jour ({last_date}) -- rien a faire.")
            return

    print(f"nav_latest.json pas encore mis a jour pour {today_str} -- calcul via sika...")

    try:
        import calc_nav
        result = calc_nav.run(quiet=False)
        if result is None:
            print("[ERREUR] calc_nav.run() a retourne None")
            sys.exit(1)
    except Exception as e:
        print(f"[ERREUR] calc_nav : {e}")
        sys.exit(1)

    print(f"[OK] nav_latest.json mis a jour")
    print(f"     VL par part   : {result.get('vl_par_part_fcfa', 0):,.0f} FCFA")


if __name__ == "__main__":
    run()
