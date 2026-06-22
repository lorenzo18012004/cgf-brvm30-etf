import sys, os, json, argparse, warnings
warnings.filterwarnings("ignore")

from datetime import datetime, time as dtime, timezone

from base import BaseScript


class IntradayScraperCloud(BaseScript):
    def __init__(self):
        super().__init__()
        self.MARKET_OPEN  = dtime(9,  0)
        self.MARKET_CLOSE = dtime(15, 30)
        self.INTRADAY_FILE = os.path.join(self.data_dir, "intraday_nav.json")
        self.HIST_FILE     = os.path.join(self.data_dir, "nav_intraday_history.json")

    def _is_market_open(self) -> bool:
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:
            return False
        t = now.time().replace(tzinfo=None)
        return self.MARKET_OPEN <= t <= self.MARKET_CLOSE

    def run(self, force: bool = False):
        os.chdir(self.data_dir)
        sys.path.insert(0, self.scripts_dir)

        now_utc   = datetime.now(timezone.utc)
        today_str = now_utc.strftime("%Y-%m-%d")

        if not force and not self._is_market_open():
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
            nav_path    = os.path.join(self.data_dir, "nav_latest.json")
            launch_path = os.path.join(self.data_dir, "launch_state.json")
            with open(nav_path, encoding="utf-8") as _f:
                _nl = json.load(_f)
            _ls      = json.load(open(launch_path, encoding="utf-8")) if os.path.exists(launch_path) else {}
            _basket  = _nl.get("basket", [])
            _nav_base = _nl["nav_indice"]
            _vl_base  = _nl.get("vl_par_part_fcfa", _nl.get("par_fcfa", 100000))

            _total_ret = 0.0
            _n_live    = 0
            _prices_now = {}
            for _item in _basket:
                _tk = _item["ticker"]
                _w  = _item["poids_pct"] / 100.0
                _p0 = _item.get("dernier_prix")
                _p1 = float(live_prices[_tk]) if _tk in live_prices.index else None
                if _p1:
                    _prices_now[_tk] = round(_p1, 0)
                if _p0 and _p0 > 0 and _p1 and _p1 > 0:
                    _total_ret += _w * (_p1 / _p0 - 1)
                    _n_live += 1

            _brvm30_rebal = {}
            _rebal_date   = None
            try:
                with open(os.path.join(self.data_dir, "rebal_detail.json"), encoding="utf-8") as _f2:
                    _rd = json.load(_f2)
                _rebals = [r for r in _rd.get("rebalancings", []) if not r.get("skipped") and r.get("basket")]
                if _rebals:
                    _last_rebal = _rebals[-1]
                    _rebal_date = _last_rebal["date"]
                    for _rb in _last_rebal["basket"]:
                        _brvm30_rebal[_rb["ticker"]] = _rb["w_brvm30"]
                    for _ex in _last_rebal.get("excluded", []):
                        _brvm30_rebal[_ex["ticker"]] = _ex["w_brvm30"]
            except Exception:
                pass

            _price_at_rebal = {}
            if _rebal_date:
                try:
                    from datetime import date as _date, timedelta as _td
                    _rh_path = os.path.join(self.data_dir, "richbourse_history.json")
                    if os.path.exists(_rh_path):
                        with open(_rh_path, encoding="utf-8") as _f3:
                            _rh = json.load(_f3)
                        for _tk in _brvm30_rebal:
                            _hist = _rh.get(_tk, {})
                            for _delta in range(0, 5):
                                _d = (_date.fromisoformat(_rebal_date) + _td(days=_delta)).isoformat()
                                if _d in _hist:
                                    _price_at_rebal[_tk] = _hist[_d].get("close") or _hist[_d].get("close_adj")
                                    break
                except Exception:
                    pass

            _denom = sum(
                _brvm30_rebal[_tk] * (_prices_now.get(_tk, 0) / _price_at_rebal[_tk])
                for _tk in _brvm30_rebal if _tk in _price_at_rebal and _price_at_rebal[_tk]
            )
            _brvm30_adj = {}
            for _tk, _w_rebal in _brvm30_rebal.items():
                if _tk in _price_at_rebal and _price_at_rebal[_tk] and _denom > 0:
                    _brvm30_adj[_tk] = _w_rebal * (_prices_now.get(_tk, 0) / _price_at_rebal[_tk]) / _denom
                else:
                    _brvm30_adj[_tk] = _w_rebal

            _nav_live   = _nav_base * (1.0 + _total_ret)
            _nav_anchor = float(_ls["nav_index_at_launch"]) if _ls and _ls.get("nav_index_at_launch") else None
            if _nav_anchor:
                _vl_live = float(_ls["par_fcfa"]) * (_nav_live / _nav_anchor)
                _n_parts = _ls.get("n_parts", _nl.get("n_parts", 50000))
            else:
                # Pas encore lancé — on fixe l'ancre maintenant et on la sauvegarde
                _nav_anchor = _nav_live
                _par        = float((_ls or {}).get("par_fcfa", _vl_base))
                _vl_live    = _par
                _n_parts    = _nl.get("n_parts", 50000)
                if _ls:
                    _ls["nav_index_at_launch"] = round(_nav_anchor, 6)
                    with open(os.path.join(self.data_dir, "launch_state.json"), "w", encoding="utf-8") as _fw:
                        json.dump(_ls, _fw, ensure_ascii=False, indent=2)
                    print(f"[LANCEMENT] nav_index_at_launch fixé à {_nav_anchor:.6f}")

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

        if os.path.exists(self.INTRADAY_FILE):
            with open(self.INTRADAY_FILE, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"date": None, "snapshots": []}

        if data.get("date") != today_str:
            data = {"date": today_str, "snapshots": [], "open_nav": nav_result["nav_indice"]}

        open_nav   = data.get("open_nav", nav_result["nav_indice"])
        change_day = (nav_result["nav_indice"] / open_nav - 1) * 100

        launch_file = os.path.join(self.data_dir, "launch_state.json")
        vl_live     = nav_result.get("vl_par_part_fcfa", 0)
        perf_launch = None
        if os.path.exists(launch_file):
            with open(launch_file, encoding="utf-8") as f:
                ls = json.load(f)
            nav_anchor  = float(ls.get("nav_index_at_launch", nav_result["nav_indice"]))
            par_fcfa    = float(ls.get("par_fcfa", 100_000))
            vl_live     = round(par_fcfa * (nav_result["nav_indice"] / nav_anchor), 0)
            perf_launch = round((nav_result["nav_indice"] / nav_anchor - 1) * 100, 4)

        _all_tickers = set(it["ticker"] for it in _basket) | set(_brvm30_rebal.keys())
        for _tk in _all_tickers:
            if _tk not in _prices_now and _tk in live_prices.index:
                _prices_now[_tk] = round(float(live_prices[_tk]), 0)

        # ── BRVM30* : reconstruction temps réel (poids BRVM30 officiels) ─────
        _p0_etf_map      = {it["ticker"]: it.get("dernier_prix") for it in _basket}
        _brvm30_star_ret = 0.0
        _brvm30_star_w   = 0.0
        for _tk, _w_b30 in _brvm30_rebal.items():
            _p0 = _p0_etf_map.get(_tk) or _price_at_rebal.get(_tk)
            _p1 = _prices_now.get(_tk)
            if _w_b30 > 0 and _p0 and _p0 > 0 and _p1 and _p1 > 0:
                _brvm30_star_ret += _w_b30 * (_p1 / _p0 - 1)
                _brvm30_star_w   += _w_b30
        if _brvm30_star_w > 0:
            _brvm30_star_ret /= _brvm30_star_w
        _brvm30_star = round(_nav_base * (1.0 + _brvm30_star_ret), 4)

        _w_etf_map   = {it["ticker"]: it["poids_pct"] / 100 for it in _basket}
        _prev_prices = {}
        _prev_snaps  = [s for s in data.get("snapshots", []) if s.get("prices_by_ticker")]
        if _prev_snaps:
            _prev_prices = _prev_snaps[-1].get("prices_by_ticker", {})

        _contribs = {}
        if _prev_prices:
            for _item in _basket:
                _tk = _item["ticker"]
                _p1 = _prev_prices.get(_tk)
                _p2 = _prices_now.get(_tk)
                if _p1 and _p2 and _p1 > 0:
                    _ret      = (_p2 / _p1 - 1) * 100
                    _w_etf    = _w_etf_map.get(_tk, _item["poids_pct"] / 100)
                    _w_brvm30 = _brvm30_adj.get(_tk, _brvm30_rebal.get(_tk, _w_etf))
                    _contribs[_tk] = {
                        "w_pct":          round(_w_etf * 100, 2),
                        "w_brvm30_pct":   round(_w_brvm30 * 100, 2),
                        "prix_prev":      _p1,
                        "prix_now":       _p2,
                        "ret_pct":        round(_ret, 3),
                        "contrib_pct":    round(_w_etf * _ret, 4),
                        "gap_contrib_pct": round((_w_etf - _w_brvm30) * _ret, 4),
                    }
            for _tk, _w_rebal in _brvm30_rebal.items():
                if _w_etf_map.get(_tk, 0) > 0 or _tk in _contribs:
                    continue
                _p1 = _prev_prices.get(_tk)
                _p2 = _prices_now.get(_tk)
                if _p1 and _p2 and _p1 > 0:
                    _ret = (_p2 / _p1 - 1) * 100
                    _w_b = _brvm30_adj.get(_tk, _w_rebal)
                    _contribs[_tk] = {
                        "w_pct": 0.0, "w_brvm30_pct": round(_w_b * 100, 2),
                        "prix_prev": _p1, "prix_now": _p2,
                        "ret_pct": round(_ret, 3),
                        "contrib_pct": 0.0,
                        "gap_contrib_pct": round(-_w_b * _ret, 4),
                    }

        snapshot = {
            "time":                now_utc.strftime("%H:%M"),
            "nav_indice":          nav_result["nav_indice"],
            "brvm30_official":     round(brvm30_val, 4) if brvm30_val else None,
            "brvm30_star":         _brvm30_star,
            "vl_par_part":         nav_result["vl_par_part_fcfa"],
            "vl_live_fcfa":        vl_live,
            "perf_since_launch":   perf_launch,
            "change_1d_pct":       nav_result["change_1d_pct"],
            "change_day_pct":      round(change_day, 4),
            "aum_mfcfa":           nav_result["aum_mfcfa"],
            "n_prices":            nav_result["n_live_prices"],
            "prices_by_ticker":    _prices_now,
            "ticker_contributions": _contribs,
        }

        existing_times = {s["time"] for s in data["snapshots"]}
        if snapshot["time"] not in existing_times:
            data["snapshots"].append(snapshot)

        with open(self.INTRADAY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        try:
            hist = json.load(open(self.HIST_FILE, encoding="utf-8")) if os.path.exists(self.HIST_FILE) else {}
            if today_str not in hist:
                hist[today_str] = []
            if snapshot["time"] not in {p["time"] for p in hist[today_str]}:
                _hist_snap = {
                    "time":                snapshot["time"],
                    "vl":                  round(vl_live, 0),
                    "vl_fcfa":             round(vl_live, 0),
                    "nav_indice":          snapshot["nav_indice"],
                    "brvm30_official":     snapshot["brvm30_official"],
                    "brvm30_star":         snapshot.get("brvm30_star"),
                    "perf_since_launch":   snapshot["perf_since_launch"],
                    "change_1d_pct":       snapshot["change_1d_pct"],
                    "change_day_pct":      snapshot["change_day_pct"],
                    "aum_mfcfa":           snapshot["aum_mfcfa"],
                    "n_prices":            snapshot["n_prices"],
                }
                if snapshot.get("ticker_contributions"):
                    _hist_snap["ticker_contributions"] = snapshot["ticker_contributions"]
                hist[today_str].append(_hist_snap)
            with open(self.HIST_FILE, "w", encoding="utf-8") as f:
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
    result = IntradayScraperCloud().run(force=args.force)
    if result is None and not args.force:
        print("Utilisez --force pour tester hors heures de marche.")
