"""
Sauvegarde le détail complet de chaque rebalancement dans rebal_detail.json.
Usage: py save_rebal_detail.py
Prérequis: test.py exécuté au moins une fois (fichiers Excel présents).
"""

import os, json, warnings
warnings.filterwarnings('ignore')

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.normpath(os.path.join(_SCRIPTS_DIR, "..", "data"))
os.chdir(BASE_DIR)

import pandas as pd
import numpy as np
import test as _m

OUTPUT_FILE = os.path.join(BASE_DIR, 'rebal_detail.json')


def _build_div_calendar(data: dict) -> dict:
    divs_net = data.get('divs_net')
    if divs_net is not None and not divs_net.empty:
        return _m.build_exdiv_calendar(data['prices'], divs_net)
    return {}


def main():
    print("Chargement donnees...")
    data = _m.load_consolidated(_m.DATA_FILE)

    # ADV et volumes depuis sika_history.json
    adv     = _m._loader.load_adv_sika()
    volumes = _m._loader.load_volumes_sika()

    prices        = data['prices']
    bench         = data['bench']
    offic_weights = data['offic_weights']
    sectors       = data['sectors']
    composition   = data['composition']
    caps          = data.get('caps')

    all_rebal   = sorted(offic_weights.columns)
    adv_by_date = _m._loader.load_adv_by_dates(all_rebal)
    print(f"  ADV Sika precalcule pour {len(adv_by_date)} dates de rebalancement")
    print(f"  Volumes Sika: {len(volumes.columns) if not volumes.empty else 0} tickers  "
          f"{'(stale par volume)' if not volumes.empty else '(fallback prix)'}")

    screening = _m.screen_universe(
        prices, caps, sectors, composition,
        adv=adv, volumes=volumes, offic_weights=offic_weights)
    data['screening'] = screening

    div_calendar = _build_div_calendar(data)

    adv_sd1  = adv_by_date.get(all_rebal[0], adv)
    w_warmup = _m._get_warmup_weights(
        offic_weights, composition, screening, sectors,
        adv_sd1, caps, prices, all_rebal[0], volumes=volumes)

    print("Backtest en cours...")
    r = _m.run_quarterly_backtest(
        prices=prices, bench=bench,
        offic_weights=offic_weights, screening=screening,
        sectors=sectors, composition=composition,
        adv=adv, adv_by_date=adv_by_date, volumes=volumes, caps=caps,
        div_calendar=div_calendar if div_calendar else None,
        start_date=all_rebal[0], end_date=all_rebal[-1],
        initial_weights=w_warmup if not w_warmup.empty else None,
        verbose=False)

    w_history  = r['w_history']
    rebal_df   = r['rebal_df']
    nav_etf_ts = r.get('nav_etf', pd.Series())   # série NAV quotidienne en FCFA

    # NAV initiale (avant le 1er rebalancement)
    nav_init = float(nav_etf_ts.iloc[0]) if not nav_etf_ts.empty else _m.NAV_REF_FCFA

    rebalancings = []
    cumul_cost   = 0.0
    prev_w_etf   = pd.Series(dtype=float)   # poids ETF du rebalancement précédent

    for _, row in rebal_df.iterrows():
        d      = pd.Timestamp(row['date'])
        skipped = bool(row['skipped'])
        # Poids pré-drift (portfolio juste avant rebalancement) et NAV réelle du moteur
        _w_drift_map   = row.get('w_drift_at_rebal', {}) or {}
        _nav_engine_mf = row.get('nav_mfcfa_at_rebal', None)
        cost_bps = float(row.get('cost_bps', 0.0))
        cumul_cost += cost_bps / 10_000   # fraction de NAV

        # NAV réelle à cette date (en Mds FCFA) calculée depuis la série quotidienne
        if not nav_etf_ts.empty:
            _valid_d = nav_etf_ts.index[nav_etf_ts.index <= d]
            _nav_idx_d = float(nav_etf_ts[_valid_d[-1]]) if len(_valid_d) > 0 else 100.0
        else:
            _nav_idx_d = 100.0
        _nav_mds = round(_nav_idx_d * _m.NAV_REF_FCFA / 100 / 1e9, 4)  # indice×NAV_REF/base/Mds

        entry = {
            'date':       d.strftime('%Y-%m-%d'),
            'date_label': d.strftime('%d %b. %Y'),
            'skipped':    skipped,
            'basket_n':   int(row.get('basket_n', 0)),
            'excl_n':     int(row.get('excl_n',   0)),
            'excl_w':     round(float(row.get('excl_w',    0.0)), 6),
            'turnover':   round(float(row.get('turnover',  0.0)), 6),
            'cost_bps':   round(cost_bps, 2),
            'cost_pct':   round(cost_bps / 100, 4),
            'nav_after':  _nav_mds,   # Mds FCFA (corrigé : série NAV quotidienne)
            'te_floor':   round(float(row.get('te_floor',  0.0)), 6),
            'cumul_cost_pct': round(cumul_cost * 100, 4),
        }

        if not skipped:
            # Poids ETF à cette date
            w_dates  = sorted(w_history.keys())
            w_etf    = w_history.get(
                min(w_dates, key=lambda x: abs((x - d).total_seconds())),
                pd.Series(dtype=float))

            # Poids BRVM30 à cette date (rebalancement officiel le plus proche)
            ow_dates  = sorted(offic_weights.columns)
            ow_date   = min(ow_dates, key=lambda x: abs((x - d).total_seconds()))
            w_b30_ser = offic_weights[ow_date]

            # NAV réelle à la date du rebalancement (en M FCFA)
            # nav_etf_ts est un indice base 100 → convertir via NAV_REF_FCFA
            _nav_ref_mfcfa = _m.NAV_REF_FCFA / 1e6   # 5000 M FCFA
            if not nav_etf_ts.empty:
                _valid = nav_etf_ts.index[nav_etf_ts.index <= d]
                _nav_idx = float(nav_etf_ts[_valid[-1]]) if len(_valid) > 0 else 100.0
            else:
                _nav_idx = 100.0
            nav_mfcfa = _nav_idx * _nav_ref_mfcfa / 100.0

            # ADV richbourse pour cette date de rebalancement
            adv_d = adv_by_date.get(d, adv)

            # ── Panier ─────────────────────────────────────────────────────
            basket = []
            for ticker, w in w_etf.items():
                if w < 0.0005:
                    continue
                w_b30    = float(w_b30_ser.get(ticker, 0.0))
                adv_v    = float(adv_d.get(ticker, 0.0)) if adv_d is not None else 0.0
                adv_req  = _m._adv_required_mfcfa(w_b30) if w_b30 > 0 else 0.0
                prev_w   = float(prev_w_etf.get(ticker, 0.0))
                is_new_entry = (prev_w == 0.0 and float(w) > 0.0001)
                delta_w  = float(w) - prev_w
                trade_mf = delta_w * nav_mfcfa                              # M FCFA, signé
                # Trade réelle = delta entre poids drift (avant rebal) et poids cible
                # Pour les nouvelles entrées : trade = poids cible (depuis 0)
                if is_new_entry:
                    real_delta = float(w)
                    real_nav   = _nav_engine_mf if _nav_engine_mf else nav_mfcfa
                else:
                    drift_w  = float(_w_drift_map.get(ticker, prev_w))
                    real_delta = abs(float(w) - drift_w)
                    real_nav   = _nav_engine_mf if _nav_engine_mf else nav_mfcfa
                days_ex = real_delta * real_nav / (adv_v * 0.15) if adv_v > 0 else 0.0
                basket.append({
                    'ticker':      ticker,
                    'secteur':     sectors.get(ticker, 'Autres'),
                    'w_etf':       round(float(w),   4),
                    'w_brvm30':    round(w_b30,       4),
                    'delta':       round(float(w) - w_b30, 4),
                    'adv_mfcfa':   round(adv_v,       1),
                    'adv_req':     round(adv_req,      1),
                    'force':       is_new_entry,              # True = nouvelle entrée dans l'ETF
                    'delta_w':     round(delta_w,     4),   # variation vs rebal précédent
                    'trade_mfcfa': round(trade_mf,    1),   # + achat, - vente
                    'days_exec':   round(days_ex,     1),   # à 15% ADV/jour
                })
            basket.sort(key=lambda x: -x['w_etf'])

            # ── Exclus ─────────────────────────────────────────────────────
            # Source de vérité : composition officielle (feuille Composition_BRVM30)
            _valid_comp = [k for k in composition if k <= d]
            _comp_date  = max(_valid_comp) if _valid_comp else min(composition.keys())
            _brvm30_set = set(composition[_comp_date])

            in_basket = {b['ticker'] for b in basket}
            excluded  = []
            for ticker, w_b30 in w_b30_ser.items():
                if pd.isna(w_b30) or w_b30 < 0.001:   # NaN = hors BRVM30 à cette date
                    continue
                if ticker not in _brvm30_set:          # pas dans la composition officielle
                    continue
                if ticker in in_basket:
                    continue
                adv_v    = float(adv_d.get(ticker, 0.0)) if adv_d is not None else 0.0
                adv_req  = _m._adv_required_mfcfa(float(w_b30)) if w_b30 > 0 else 0.0
                scr_row  = screening.loc[ticker] if ticker in screening.index else None
                raison   = str(scr_row['raison']) if (scr_row is not None and 'raison' in scr_row.index) else 'N/A'
                prev_w   = float(prev_w_etf.get(ticker, 0.0))
                trade_mf = -prev_w * nav_mfcfa   # vente de la position précédente
                days_ex  = abs(trade_mf) / (adv_v * 0.15) if (adv_v > 0 and prev_w > 0) else 0.0
                excluded.append({
                    'ticker':      ticker,
                    'secteur':     sectors.get(ticker, 'Autres'),
                    'w_brvm30':    round(float(w_b30), 4),
                    'adv_mfcfa':   round(adv_v,         1),
                    'adv_req':     round(adv_req,        1),
                    'ratio_adv':   round(adv_v / adv_req, 2) if adv_req > 0 else 0.0,
                    'raison':      raison,
                    'prev_w':      round(prev_w,          4),  # poids ETF avant ce rebal
                    'trade_mfcfa': round(trade_mf,         1), # vente (négatif si vente)
                    'days_exec':   round(days_ex,           1),
                })
            excluded.sort(key=lambda x: -x['w_brvm30'])

            entry['basket']   = basket
            entry['excluded'] = excluded

            # Couverture (somme des poids ETF des stocks qui sont aussi dans BRVM30)
            cov = sum(b['w_brvm30'] for b in basket)
            entry['coverage'] = round(cov, 4)

            # ── Vérification poids ─────────────────────────────────────────
            _sum_w   = sum(b['w_brvm30'] for b in basket) + sum(e['w_brvm30'] for e in excluded)
            _n_zero  = [b['ticker'] for b in basket if b['w_brvm30'] < 0.0003]
            _ok      = abs(_sum_w - 1.0) < 0.02 and not _n_zero
            entry['weight_check'] = {
                'sum_brvm30_weights': round(_sum_w, 4),
                'ok': _ok,
                'tickers_w_zero': _n_zero,
            }
            _status = "✓" if _ok else "✗"
            _warn   = f"  WARN: {_n_zero} ont w=0" if _n_zero else ""
            print(f"  [{d.strftime('%d/%m/%Y')}] Poids: somme={_sum_w:.1%} | panier={len(basket)} | exclus={len(excluded)} {_status}{_warn}")

            # Mémoriser les poids ETF pour le prochain rebalancement
            prev_w_etf = w_etf.copy()

        rebalancings.append(entry)
        if skipped:
            detail_str = 'ANNULE'
        else:
            detail_str = (f"Panier={entry['basket_n']}  Exclus={entry['excl_n']}"
                          f"  TO={entry['turnover']:.1%}  Cout={entry['cost_bps']:.0f}bps")
        print(f"  {entry['date_label']:15s}  {detail_str}")

    # Résumé global
    eff = [x for x in rebalancings if not x['skipped']]
    summary = {
        'n_rebal':         len(rebalancings),
        'n_effective':     len(eff),
        'n_skipped':       len(rebalancings) - len(eff),
        'turnover_avg':    round(np.mean([x['turnover'] for x in eff]), 4) if eff else 0.0,
        'cost_bps_avg':    round(np.mean([x['cost_bps'] for x in eff]), 1) if eff else 0.0,
        'cost_bps_total':  round(sum(x['cost_bps'] for x in eff), 1),
        'basket_n_avg':    round(np.mean([x['basket_n'] for x in eff]), 1) if eff else 0.0,
        'excl_w_avg':      round(np.mean([x['excl_w'] for x in eff]), 4) if eff else 0.0,
    }

    # ── Entrées / sorties de composition BRVM30 ───────────────────────────────
    comp_hist_file = os.path.join(BASE_DIR, 'brvm_composition_history.json')
    if os.path.exists(comp_hist_file):
        with open(comp_hist_file, encoding='utf-8') as _f:
            comp_hist_raw = json.load(_f)
        comp_hist = sorted(
            [e for e in comp_hist_raw if e.get('rebal_date')],
            key=lambda x: x['rebal_date']
        )
        comp_by_date = {e['rebal_date']: set(e.get('composition', [])) for e in comp_hist}
        sorted_dates = sorted(comp_by_date.keys())

        from datetime import datetime as _dt
        def _nearest(d_str):
            if d_str in comp_by_date:
                return d_str
            d_ts = _dt.strptime(d_str, '%Y-%m-%d')
            closest = min(sorted_dates, key=lambda x: abs((_dt.strptime(x,'%Y-%m-%d') - d_ts).days))
            return closest if abs((_dt.strptime(closest,'%Y-%m-%d') - d_ts).days) <= 7 else None

        for entry in rebalancings:
            d = _nearest(entry['date'])
            if d is None:
                continue
            cur_set = comp_by_date[d]
            idx = sorted_dates.index(d)
            if idx == 0:
                entry['entries'] = []
                entry['exits']   = []
            else:
                prev_set = comp_by_date[sorted_dates[idx - 1]]
                entry['entries'] = sorted(cur_set - prev_set)
                entry['exits']   = sorted(prev_set - cur_set)

    output = {'summary': summary, 'rebalancings': rebalancings}

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nFichier sauvegardé : {OUTPUT_FILE}")
    print(f"  {len(rebalancings)} rebalancements ({len(eff)} effectifs, {len(rebalancings)-len(eff)} annulés)")
    print(f"  Turnover moyen     : {summary['turnover_avg']:.2%}")
    print(f"  Coût moyen/rebal   : {summary['cost_bps_avg']:.1f} bps")
    print(f"  Coût total cumulé  : {summary['cost_bps_total']:.0f} bps = {summary['cost_bps_total']/100:.2f}%")


if __name__ == '__main__':
    main()
