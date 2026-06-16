"""
scrape_intraday_cloud.py -- iNAV intraday (version cloud / GitHub Actions)
=========================================================================
Identique a scrape_intraday.py mais utilise live_nav_cloud.py au lieu de
live_nav.py, ce qui supprime la dependance a l Excel local.

Fichiers requis dans le repo :
  richbourse_history.json, rebal_detail.json, nav_latest.json,
  launch_state.json (optionnel)

Usage :
    python scrape_intraday_cloud.py          # respecte les heures de marche
    python scrape_intraday_cloud.py --force  # force meme hors marche
"""
import sys, os, json, argparse, warnings
warnings.filterwarnings("ignore")

from datetime import datetime, time as dtime, timezone

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
INTRADAY_FILE = os.path.join(BASE_DIR, "intraday_nav.json")
HIST_FILE     = os.path.join(BASE_DIR, "nav_intraday_history.json")

MARKET_OPEN  = dtime(9,  0)
MARKET_CLOSE = dtime(15, 30)


def _is_market_open() -> bool:
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    t = now.time().replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def run(force: bool = False):
    os.chdir(BASE_DIR)
    sys.path.insert(0, BASE_DIR)

    now_utc   = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    if not force and not _is_market_open():
        print(f"[{now_utc.strftime('%H:%M')} UTC] Hors heures de marche BRVM (09h00-15h30) -- rien a faire.")
        return None

    try:
        from scrape_sika import _fetch_html, scrape_prices, scrape_brvm30_index, SIKA_URL
        html        = _fetch_html(SIKA_URL)
        live_prices = scrape_prices(html)
        brvm30_val  = scrape_brvm30_index(html)
    except Exception as e:
        print(f"[ERREUR] Scraping sika : {e}")
        return None

    try:
        nav_path    = os.path.join(BASE_DIR, "nav_latest.json")
        launch_path = os.path.join(BASE_DIR, "launch_state.json")
        with open(nav_path, encoding="utf-8") as _f:
            _nl = json.load(_f)
        _ls      = json.load(open(launch_path, encoding="utf-8")) if os.path.exists(launch_path) else {}
        _basket  = _nl.get("basket", [])
        _nav_base = _nl["nav_indice"]
        _vl_base  = _nl.get("vl_par_part_fcfa", _nl.get("par_fcfa", 100000))

        _total_ret = 0.0
        _n_live    = 0
        for _item in _basket:
            _tk = _item["ticker"]
            _w  = _item["poids_pct"] / 100.0
            _p0 = _item.get("dernier_prix")
            _p1 = float(live_prices[_tk]) if _tk in live_prices.index else None
            if _p0 and _p0 > 0 and _p1 and _p1 > 0:
                _total_ret += _w * (_p1 / _p0 - 1)
                _n_live += 1

        _nav_live = _nav_base * (1.0 + _total_ret)
        if _ls:
            _vl_live = float(_ls["par_fcfa"]) * (_nav_live / float(_ls["nav_index_at_launch"]))
            _n_parts = _ls["n_parts"]
        else:
            _vl_live = _vl_base * (1.0 + _total_ret)
            _n_parts = _nl.get("n_parts", 50000)

        nav_result = {
            "nav_indice":       round(_nav_live, 4),
            "vl_par_part_fcfa": round(_vl_live, 0),
            "change_1d_pct":    round((_nav_live / _nav_base - 1) * 100, 4),
            "aum_mfcfa":        round(_vl_live * _n_parts / 1_000_000, 1),
            "n_live_prices":    _n_live,
        }
    except Exception as e:
        print(f"[ERREUR] Calcul VL : {e}")
        return None

    if os.path.exists(INTRADAY_FILE):
        with open(INTRADAY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"date": None, "snapshots": []}

    if data.get("date") != today_str:
        data = {"date": today_str, "snapshots": [], "open_nav": nav_result["nav_indice"]}

    open_nav   = data.get("open_nav", nav_result["nav_indice"])
    change_day = (nav_result["nav_indice"] / open_nav - 1) * 100

    launch_file = os.path.join(BASE_DIR, "launch_state.json")
    vl_live     = nav_result.get("vl_par_part_fcfa", 0)
    perf_launch = None
    if os.path.exists(launch_file):
        with open(launch_file, encoding="utf-8") as f:
            ls = json.load(f)
        nav_anchor  = float(ls.get("nav_index_at_launch", nav_result["nav_indice"]))
        par_fcfa    = float(ls.get("par_fcfa", 100_000))
        vl_live     = round(par_fcfa * (nav_result["nav_indice"] / nav_anchor), 0)
        perf_launch = round((nav_result["nav_indice"] / nav_anchor - 1) * 100, 4)

    snapshot = {
        "time":              now_utc.strftime("%H:%M"),
        "nav_indice":        nav_result["nav_indice"],
        "brvm30_official":   round(brvm30_val, 4) if brvm30_val else None,
        "vl_par_part":       nav_result["vl_par_part_fcfa"],
        "vl_live_fcfa":      vl_live,
        "perf_since_launch": perf_launch,
        "change_1d_pct":     nav_result["change_1d_pct"],
        "change_day_pct":    round(change_day, 4),
        "aum_mfcfa":         nav_result["aum_mfcfa"],
        "n_prices":          nav_result["n_live_prices"],
    }

    existing_times = {s["time"] for s in data["snapshots"]}
    if snapshot["time"] not in existing_times:
        data["snapshots"].append(snapshot)

    with open(INTRADAY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    try:
        hist = json.load(open(HIST_FILE, encoding="utf-8")) if os.path.exists(HIST_FILE) else {}
        if today_str not in hist:
            hist[today_str] = []
        if snapshot["time"] not in {p["time"] for p in hist[today_str]}:
            hist[today_str].append({
                "time":              snapshot["time"],
                "vl":                round(vl_live, 0),
                "vl_fcfa":           round(vl_live, 0),
                "nav_indice":        snapshot["nav_indice"],
                "perf_since_launch": snapshot["perf_since_launch"],
                "change_1d_pct":     snapshot["change_1d_pct"],
                "change_day_pct":    snapshot["change_day_pct"],
                "aum_mfcfa":         snapshot["aum_mfcfa"],
                "n_prices":          snapshot["n_prices"],
            })
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Historique : {e}")

    launch_str = f" | Dlancement {perf_launch:+.3f}%" if perf_launch is not None else ""
    print(f"[{snapshot['time']} UTC] iNAV {nav_result['nav_indice']:.4f} | VL {vl_live:,.0f} FCFA | Djour {change_day:+.3f}%{launch_str}")
    return snapshot


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="iNAV CGF BRVM30 ETF -- cloud (no Excel)")
    parser.add_argument("--force", action="store_true", help="Forcer meme hors heures de marche")
    args = parser.parse_args()
    result = run(force=args.force)
    if result is None and not args.force:
        print("Utilisez --force pour tester hors heures de marche.")
