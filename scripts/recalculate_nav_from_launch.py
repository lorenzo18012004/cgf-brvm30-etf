"""
recalculate_nav_from_launch.py
Recalcule la NAV depuis le lancement (2026-06-19) avec la méthode de caps actuelle (30 titres).
Corrige la série live qui mélangeait l'ancienne méthode (exclusions ADV) et la nouvelle (caps).
Usage : python scripts/recalculate_nav_from_launch.py
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))

from base import BaseScript
from rebalance_live import build_adv_capped_weights

MGMT_FEE_ANN = 0.006
FEE_DAILY    = (1.0 - MGMT_FEE_ANN) ** (1.0 / 252.0)


class NavRecalculator(BaseScript):

    def _get_close(self, sh, ticker, date):
        v = sh.get(ticker, {}).get(date)
        if v is None:
            return None
        return float(v['close'] if isinstance(v, dict) else v)

    def run(self):
        nl = self.load_json_path(os.path.join(self.data_dir, 'nav_latest.json'))
        sh = self.load_json_path(os.path.join(self.data_dir, 'sika_history.json'))
        ls = self.load_json_path(os.path.join(self.data_dir, 'launch_state.json'))

        # Poids BRVM30 officiels extraits du basket actuel
        basket_items = nl['basket']
        w_brvm30 = {item['ticker']: item['w_brvm30']
                    for item in basket_items if item.get('w_brvm30')}

        launch_date   = ls['launch_date']          # "2026-06-19"
        nav_at_launch = float(ls['nav_index_at_launch'])  # 243.70698
        par_fcfa      = float(ls['par_fcfa'])       # 100 000
        aum_mfcfa     = float(ls['aum_initial_mfcfa'])    # 5 000
        n_parts       = int(ls['n_parts'])           # 50 000

        # ── Jours de trading depuis le lancement ──────────────────────────────
        all_dates = set()
        for tk in w_brvm30:
            if tk in sh:
                all_dates.update(d for d in sh[tk] if d >= launch_date)
        # Garder uniquement les jours où au moins 25 des 30 titres ont un prix
        trading_days = sorted(
            d for d in all_dates
            if sum(1 for tk in w_brvm30 if d in sh.get(tk, {})) >= 25
        )
        print(f"Jours de trading depuis {launch_date} : {len(trading_days)}")
        print(f"  Premier : {trading_days[0]}  Dernier : {trading_days[-1]}")

        # ── Basket initial : caps ADV recalculés au 19 juin ───────────────────
        init_weights, exclu_info, otc_set = build_adv_capped_weights(
            w_brvm30, launch_date, aum_mfcfa, sh
        )
        print(f"\nBasket initial recalculé au {launch_date} ({len(init_weights)} titres) :")
        for tk, w in sorted(init_weights.items(), key=lambda x: -x[1]):
            flag = " [OTC]" if tk in otc_set else ""
            print(f"  {tk}: {w*100:.2f}%{flag}")
        if exclu_info:
            print(f"  Exclus : {exclu_info}")

        # ── Itération jour par jour ───────────────────────────────────────────
        nav_series   = {}
        weights      = dict(init_weights)
        nav          = nav_at_launch
        prev_prices  = {tk: self._get_close(sh, tk, trading_days[0])
                        for tk in weights}

        nav_series[trading_days[0]] = round(nav, 6)

        for date in trading_days[1:]:
            today_prices = {tk: self._get_close(sh, tk, date) for tk in weights}

            # Mark-to-market
            new_weights = {}
            total = 0.0
            for tk, w in weights.items():
                p0 = prev_prices.get(tk)
                p1 = today_prices.get(tk)
                new_w = w * (p1 / p0) if (p0 and p0 > 0 and p1 and p1 > 0) else w
                new_weights[tk] = new_w
                total += new_w

            nav = nav * total * FEE_DAILY
            nav_series[date] = round(nav, 6)

            # Poids normalisés pour le jour suivant
            weights     = {tk: w / total for tk, w in new_weights.items()}
            prev_prices = today_prices

        # ── Résultat ──────────────────────────────────────────────────────────
        print(f"\nSérie NAV recalculée :")
        for d, v in sorted(nav_series.items()):
            vl   = par_fcfa * (v / nav_at_launch)
            perf = (v / nav_at_launch - 1) * 100
            print(f"  {d} : nav={v:.4f}  VL={vl:,.0f} FCFA  perf={perf:+.3f}%")

        last_date = max(nav_series)
        last_nav  = nav_series[last_date]
        vl_final  = par_fcfa * (last_nav / nav_at_launch)
        perf_final = (last_nav / nav_at_launch - 1) * 100

        # ── Mise à jour nav_latest.json ───────────────────────────────────────
        for item in basket_items:
            tk = item['ticker']
            if tk in weights:
                item['poids_pct'] = round(weights[tk] * 100, 4)

        nl['nav_live_series'] = [[d, v] for d, v in sorted(nav_series.items())]
        nl.update({
            'nav_indice':        round(last_nav, 4),
            'vl_par_part_fcfa':  round(vl_final, 0),
            'perf_since_launch': round(perf_final, 4),
            'calc_date':         last_date,
        })

        self.save_json_path(os.path.join(self.data_dir, 'nav_latest.json'), nl)

        print(f"\n[OK] nav_latest.json mis à jour (méthode caps, {len(init_weights)} titres).")
        print(f"     NAV indice   : {last_nav:.4f}")
        print(f"     VL par part  : {vl_final:,.0f} FCFA")
        print(f"     Perf lct     : {perf_final:+.3f}%")
        print(f"     Poids OK     : {sum(weights.values()):.6f} (doit être ~1.0)")


if __name__ == '__main__':
    NavRecalculator().run()
