"""
calc_nav_cloud.py -- Fallback NAV quotidien (GitHub Actions, sans Excel)
=======================================================================
Calcule la VL directement depuis nav_latest.json (basket + prix J-1)
et sika_history.json (prix de clôture du jour), sans passer par _extend_nav.
"""
import sys, os, json, warnings
warnings.filterwarnings("ignore")

from datetime import datetime, timezone

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
NAV_PATH   = os.path.join(BASE_DIR, "nav_latest.json")
SIKA_PATH  = os.path.join(BASE_DIR, "sika_history.json")
LAUNCH_PATH = os.path.join(BASE_DIR, "launch_state.json")


def run() -> None:
    os.chdir(BASE_DIR)

    now_utc   = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    if not os.path.exists(NAV_PATH):
        print("[ERREUR] nav_latest.json introuvable")
        sys.exit(1)

    with open(NAV_PATH, encoding="utf-8") as f:
        nl = json.load(f)

    last_date = nl.get("calc_date") or nl.get("date", "")
    if last_date >= today_str:
        print(f"[OK] nav_latest.json deja a jour ({last_date}) -- rien a faire.")
        return

    print(f"nav_latest.json pas encore mis a jour pour {today_str} -- calcul via sika...")

    # Charger sika_history pour trouver les prix de clôture du dernier jour
    if not os.path.exists(SIKA_PATH):
        print("[ERREUR] sika_history.json introuvable")
        sys.exit(1)

    with open(SIKA_PATH, encoding="utf-8") as f:
        sika = json.load(f)

    # Prix les plus récents par ticker
    latest_prices = {}
    for ticker, hist in sika.items():
        if hist:
            last_d = max(hist.keys())
            v = hist[last_d]
            close = v.get("close") if isinstance(v, dict) else v
            if close:
                latest_prices[ticker] = float(close)

    # Prix de référence J-1 depuis le basket de nav_latest.json
    basket = nl.get("basket", [])
    if not basket:
        print("[ERREUR] basket vide dans nav_latest.json")
        sys.exit(1)

    nav_base  = float(nl["nav_indice"])
    vl_base   = float(nl.get("vl_par_part_fcfa") or nl.get("par_fcfa", 100000))
    n_parts   = int(nl.get("n_parts", 50000))

    # Rendement pondéré depuis les prix J-1 du basket
    total_ret = 0.0
    n_live    = 0
    for item in basket:
        tk = item["ticker"]
        w  = item["poids_pct"] / 100.0
        p0 = item.get("dernier_prix")
        p1 = latest_prices.get(tk)
        if p0 and p0 > 0 and p1 and p1 > 0:
            total_ret += w * (p1 / p0 - 1)
            n_live += 1

    nav_new = nav_base * (1.0 + total_ret)

    # VL depuis le lancement
    if os.path.exists(LAUNCH_PATH):
        with open(LAUNCH_PATH, encoding="utf-8") as f:
            ls = json.load(f)
        nav_anchor = ls.get("nav_index_at_launch")
        n_parts    = int(ls.get("n_parts", n_parts))
        par_ls     = float(ls.get("par_fcfa", 100_000))
        if not nav_anchor:
            # Premier calcul depuis le lancement — ancrer la NAV courante
            nav_anchor = nav_new
            ls["nav_index_at_launch"] = round(float(nav_new), 6)
            with open(LAUNCH_PATH, "w", encoding="utf-8") as fw:
                json.dump(ls, fw, ensure_ascii=False, indent=2)
            print(f"[LANCEMENT] nav_index_at_launch fixé à {nav_anchor:.6f}")
        vl_new   = par_ls * (nav_new / float(nav_anchor))
        perf_lct = (nav_new / float(nav_anchor) - 1) * 100
    else:
        vl_new   = vl_base * (1.0 + total_ret)
        perf_lct = total_ret * 100

    aum = vl_new * n_parts / 1_000_000
    chg = total_ret * 100

    # Mettre à jour nav_latest.json en conservant toutes les clés existantes
    nl.update({
        "calc_date":          today_str,
        "launched":           True,
        "nav_indice":         round(nav_new, 4),
        "vl_par_part_fcfa":   round(vl_new, 0),
        "aum_mfcfa":          round(aum, 1),
        "perf_since_launch":  round(perf_lct, 4),
        "change_day_pct":     round(chg, 4),
        "source":             "cloud_fallback_sika",
        "n_live_prices":      n_live,
    })

    # Mettre à jour dernier_prix dans le basket
    for item in nl["basket"]:
        tk = item["ticker"]
        if tk in latest_prices:
            item["dernier_prix"] = latest_prices[tk]

    with open(NAV_PATH, "w", encoding="utf-8") as f:
        json.dump(nl, f, ensure_ascii=False, indent=2)

    print(f"[OK] nav_latest.json mis a jour ({today_str})")
    print(f"     NAV indice   : {nav_new:.4f}")
    print(f"     VL par part  : {vl_new:,.0f} FCFA")
    print(f"     Perf lct     : {perf_lct:+.3f}%")
    print(f"     Variation J  : {chg:+.3f}%")
    print(f"     Cours utilises: {n_live} tickers")


if __name__ == "__main__":
    run()
