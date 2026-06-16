"""
scrape_intraday.py — iNAV toutes les 15 minutes (heures BRVM)
=============================================================
Scrape Sika Finance, calcule la VL indicative intraday et
l'ajoute à intraday_nav.json SANS modifier l'Excel ni nav_latest.json.

Lancé automatiquement par le Task Scheduler.
Peut aussi être appelé manuellement : python scrape_intraday.py

Heures BRVM : 09h00 – 15h30 heure d'Abidjan (UTC+0)
"""

import sys, os, json, warnings
warnings.filterwarnings('ignore')

import pandas as pd
from datetime import datetime, time as dtime, timezone

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
INTRADAY_FILE = os.path.join(BASE_DIR, 'intraday_nav.json')
LOG_DIR       = os.path.join(BASE_DIR, 'logs')

# Heures de marché BRVM (heure Abidjan = UTC+0)
MARKET_OPEN  = dtime(9,  0)
MARKET_CLOSE = dtime(15, 30)


def _is_market_open() -> bool:
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:   # week-end
        return False
    t = now_utc.time().replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def _load_intraday() -> dict:
    if os.path.exists(INTRADAY_FILE):
        with open(INTRADAY_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'date': None, 'snapshots': []}


def _save_intraday(data: dict) -> None:
    with open(INTRADAY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _log(msg: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime('%Y%m%d')
    log_file = os.path.join(LOG_DIR, f'intraday_{today}.log')
    with open(log_file, 'a', encoding='utf-8') as f:
        ts = datetime.now().strftime('%H:%M:%S')
        f.write(f'[{ts}] {msg}\n')


def run(force: bool = False) -> dict | None:
    """
    Scrape et met à jour intraday_nav.json.
    force=True : ignore la vérification des heures de marché.
    """
    os.chdir(BASE_DIR)
    sys.path.insert(0, BASE_DIR)

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime('%Y-%m-%d')

    if not force and not _is_market_open():
        _log(f"Hors heures de marche ({now_utc.strftime('%H:%M')} UTC) — rien a faire")
        return None

    # Scraper les cours + indice BRVM30 officiel
    brvm30_official = None
    try:
        from scrape_sika import _fetch_html, scrape_prices, scrape_brvm30_index, SIKA_URL
        html = _fetch_html(SIKA_URL)
        live_prices = scrape_prices(html)
        brvm30_official = scrape_brvm30_index(html)
    except Exception as e:
        _log(f"ERREUR scraping : {e}")
        return None

    # Calculer la VL live inline (live_nav.py supprimé — calcul direct)
    try:
        nav_path = os.path.join(BASE_DIR, 'nav_latest.json')
        launch_path = os.path.join(BASE_DIR, 'launch_state.json')
        with open(nav_path, encoding='utf-8') as _f:
            _nl = json.load(_f)
        _ls = json.load(open(launch_path, encoding='utf-8')) if os.path.exists(launch_path) else {}

        _basket    = _nl.get('basket', [])
        _nav_base  = _nl['nav_indice']           # indice au dernier calcul quotidien
        _last_date = _nl.get('calc_date', '')
        _vl_base   = _nl.get('vl_par_part_fcfa', _nl.get('par_fcfa', 100000))

        # Rendement live = somme pondérée des variations de cours
        _total_ret = 0.0
        _n_live = 0
        for _item in _basket:
            _tk = _item['ticker']
            _w  = _item['poids_pct'] / 100.0
            _p0 = _item.get('dernier_prix')
            _p1 = float(live_prices[_tk]) if _tk in live_prices.index else None
            if _p0 and _p0 > 0 and _p1 and _p1 > 0:
                _total_ret += _w * (_p1 / _p0 - 1)
                _n_live += 1

        _nav_live = _nav_base * (1.0 + _total_ret)

        # VL live en FCFA
        if _ls:
            _nav_anchor = _ls['nav_index_at_launch']
            _par        = float(_ls['par_fcfa'])
            _n_parts    = _ls['n_parts']
            _vl_live    = _par * (_nav_live / _nav_anchor)
        else:
            _n_parts = _nl.get('n_parts', 50000)
            _vl_live = _vl_base * (1.0 + _total_ret)
            _nav_anchor = _nav_base

        _aum = _vl_live * _n_parts / 1_000_000

        # Variation vs veille (nav_latest = clôture J-1)
        _change_1d = (_nav_live / _nav_base - 1) * 100

        nav_result = {
            'nav_indice':      round(_nav_live, 4),
            'vl_par_part_fcfa': round(_vl_live, 0),
            'change_1d_pct':   round(_change_1d, 4),
            'aum_mfcfa':       round(_aum, 1),
            'n_live_prices':   _n_live,
        }
    except Exception as e:
        _log(f"ERREUR calcul VL : {e}")
        return None

    # Charger l'historique intraday
    data = _load_intraday()

    # Réinitialiser si nouveau jour
    if data.get('date') != today_str:
        data = {
            'date':      today_str,
            'snapshots': [],
            'open_nav':  nav_result['nav_indice'],   # VL à l'ouverture
        }

    # Calculer le rendement depuis l'ouverture
    open_nav   = data.get('open_nav', nav_result['nav_indice'])
    change_day = (nav_result['nav_indice'] / open_nav - 1) * 100

    # VL live depuis le lancement officiel
    launch_file = os.path.join(BASE_DIR, 'launch_state.json')
    vl_live = None
    perf_launch = None
    if os.path.exists(launch_file):
        with open(launch_file, encoding='utf-8') as f:
            ls = json.load(f)
        nav_anchor = ls['nav_index_at_launch']
        par_live   = float(ls['par_fcfa'])
        vl_live    = round(par_live * (nav_result['nav_indice'] / nav_anchor), 0)
        perf_launch = round((nav_result['nav_indice'] / nav_anchor - 1) * 100, 4)

    snapshot = {
        'time':              now_utc.strftime('%H:%M'),
        'nav_indice':        nav_result['nav_indice'],
        'brvm30_official':   round(brvm30_official, 4) if brvm30_official else None,
        'vl_par_part':       nav_result['vl_par_part_fcfa'],
        'vl_live_fcfa':      vl_live,
        'perf_since_launch': perf_launch,
        'change_1d_pct':     nav_result['change_1d_pct'],
        'change_day_pct':    round(change_day, 4),
        'aum_mfcfa':         nav_result['aum_mfcfa'],
        'n_prices':          nav_result['n_live_prices'],
    }

    # ── Prix et contributions par titre ──────────────────────────────────────
    try:
        nav_path = os.path.join(BASE_DIR, 'nav_latest.json')
        with open(nav_path, encoding='utf-8') as _f:
            _nl = json.load(_f)
        _basket = _nl.get('basket', [])

        # ── Poids ETF et BRVM30 sur les 30 titres ───────────────────────────
        # Poids BRVM30 ajustés à la dérive des prix depuis le rééquilibrage :
        #   w_brvm30_actuel_i = w_brvm30_rebal_i × (prix_actuel_i / prix_rebal_i) / Z
        # Cela reproduit exactement la dérive naturelle d'un indice market-cap weighted.
        _brvm30_rebal = {}   # poids BRVM30 au dernier rebal
        _rebal_date   = None
        try:
            with open(os.path.join(BASE_DIR, 'rebal_detail.json'), encoding='utf-8') as _f2:
                _rd = json.load(_f2)
            _rebals = [r for r in _rd.get('rebalancings', []) if not r.get('skipped') and r.get('basket')]
            if _rebals:
                _last_rebal = _rebals[-1]
                _rebal_date = _last_rebal['date']
                for _rb in _last_rebal['basket']:
                    _brvm30_rebal[_rb['ticker']] = _rb['w_brvm30']
                for _ex in _last_rebal.get('excluded', []):
                    _brvm30_rebal[_ex['ticker']] = _ex['w_brvm30']
        except Exception:
            pass

        # Prix de clôture au dernier rééquilibrage (depuis richbourse_history.json)
        _price_at_rebal = {}
        if _rebal_date:
            try:
                from datetime import date as _date, timedelta as _td
                with open(os.path.join(BASE_DIR, 'richbourse_history.json'), encoding='utf-8') as _f3:
                    _rh = json.load(_f3)
                for _tk in _brvm30_rebal:
                    _hist = _rh.get(_tk, {})
                    for _delta in range(0, 5):   # cherche J, J+1, J+2 ... (jours fériés/week-end)
                        _d = (_date.fromisoformat(_rebal_date) + _td(days=_delta)).isoformat()
                        if _d in _hist:
                            _price_at_rebal[_tk] = _hist[_d].get('close') or _hist[_d].get('close_adj')
                            break
            except Exception:
                pass

        # Prix live pour tous les 30 titres BRVM30
        _prices_now = {}
        for _tk in set([it['ticker'] for it in _basket]) | set(_brvm30_rebal.keys()):
            if _tk in live_prices.index:
                _prices_now[_tk] = round(float(live_prices[_tk]), 0)

        # Poids BRVM30 ajustés à la dérive des prix depuis le rebal
        # Si pas de prix historique pour un titre → on garde le poids du rebal
        _denom = sum(
            _brvm30_rebal[_tk] * (_prices_now.get(_tk, 0) / _price_at_rebal[_tk])
            for _tk in _brvm30_rebal if _tk in _price_at_rebal and _price_at_rebal[_tk]
        )
        _brvm30_adj = {}
        for _tk, _w_rebal in _brvm30_rebal.items():
            if _tk in _price_at_rebal and _price_at_rebal[_tk] and _denom > 0:
                _brvm30_adj[_tk] = _w_rebal * (_prices_now.get(_tk, 0) / _price_at_rebal[_tk]) / _denom
            else:
                _brvm30_adj[_tk] = _w_rebal  # fallback

        # Pour les ETF : poids actuels depuis nav_latest (mis à jour quotidiennement)
        _w_etf_map = {it['ticker']: it['poids_pct'] / 100 for it in _basket}

        # Prix du snapshot précédent
        _prev_prices = {}
        _prev_snaps = [s for s in data.get('snapshots', []) if s.get('prices_by_ticker')]
        if _prev_snaps:
            _prev_prices = _prev_snaps[-1].get('prices_by_ticker', {})

        # Contributions completes sur 30 titres
        _contribs = {}
        if _prev_prices:
            for _item in _basket:
                _tk = _item['ticker']
                _p1 = _prev_prices.get(_tk); _p2 = _prices_now.get(_tk)
                if _p1 and _p2 and _p1 > 0:
                    _ret = (_p2 / _p1 - 1) * 100
                    _w_etf    = _w_etf_map.get(_tk, _item['poids_pct'] / 100)
                    _w_brvm30 = _brvm30_adj.get(_tk, _brvm30_rebal.get(_tk, _w_etf))
                    _contribs[_tk] = {
                        'w_pct':           round(_w_etf * 100, 2),
                        'w_brvm30_pct':    round(_w_brvm30 * 100, 2),
                        'prix_prev':        _p1, 'prix_now': _p2,
                        'ret_pct':          round(_ret, 3),
                        'contrib_pct':      round(_w_etf * _ret, 4),
                        'gap_contrib_pct':  round((_w_etf - _w_brvm30) * _ret, 4),
                    }
            for _tk, _w_rebal in _brvm30_rebal.items():
                if _w_etf_map.get(_tk, 0) > 0 or _tk in _contribs:
                    continue
                _p1 = _prev_prices.get(_tk); _p2 = _prices_now.get(_tk)
                if _p1 and _p2 and _p1 > 0:
                    _ret = (_p2 / _p1 - 1) * 100
                    _w_b = _brvm30_adj.get(_tk, _w_rebal)
                    _contribs[_tk] = {
                        'w_pct': 0.0, 'w_brvm30_pct': round(_w_b * 100, 2),
                        'prix_prev': _p1, 'prix_now': _p2,
                        'ret_pct': round(_ret, 3),
                        'contrib_pct': 0.0,
                        'gap_contrib_pct': round(-_w_b * _ret, 4),
                    }

        snapshot['prices_by_ticker']    = _prices_now
        snapshot['ticker_contributions'] = _contribs
    except Exception as _e:
        _log(f"Contributions calc error: {_e}")
        snapshot['prices_by_ticker']    = {}
        snapshot['ticker_contributions'] = {}

    # Éviter les doublons (même minute)
    existing_times = {s['time'] for s in data['snapshots']}
    if snapshot['time'] not in existing_times:
        data['snapshots'].append(snapshot)

    # Interpoler les valeurs manquantes de brvm30_official
    # Quand un nouveau snapshot arrive avec une valeur, on remplit les trous précédents
    def _to_min(t):
        h, m = t.split(':')
        return int(h) * 60 + int(m)

    _snaps = data['snapshots']
    for _i, _s in enumerate(_snaps):
        if _s.get('brvm30_official') is not None:
            continue
        _prev = next((j for j in range(_i - 1, -1, -1) if _snaps[j].get('brvm30_official') is not None), None)
        _next = next((j for j in range(_i + 1, len(_snaps)) if _snaps[j].get('brvm30_official') is not None), None)
        if _prev is not None and _next is not None:
            _t0 = _to_min(_snaps[_prev]['time'])
            _t1 = _to_min(_snaps[_next]['time'])
            _tc = _to_min(_s['time'])
            if _t1 > _t0:
                _ratio = (_tc - _t0) / (_t1 - _t0)
                _v0 = _snaps[_prev]['brvm30_official']
                _v1 = _snaps[_next]['brvm30_official']
                _s['brvm30_official'] = round(_v0 + _ratio * (_v1 - _v0), 4)
                _log(f"Interpolation brvm30_official {_s['time']}: {_s['brvm30_official']} (entre {_snaps[_prev]['time']} et {_snaps[_next]['time']})")

    _save_intraday(data)

    # Historique intraday complet — toutes les données pour reporting
    hist_path = os.path.join(BASE_DIR, 'nav_intraday_history.json')
    try:
        hist = json.load(open(hist_path, encoding='utf-8')) if os.path.exists(hist_path) else {}
        today_key = data['date']
        if today_key not in hist:
            hist[today_key] = []
        existing_times_h = {p['time'] for p in hist[today_key]}
        if snapshot['time'] not in existing_times_h:
            vl_h = vl_live if vl_live else nav_result['vl_par_part_fcfa']
            hist[today_key].append({
                'time':              snapshot['time'],
                'vl':                round(vl_h, 0),
                'vl_fcfa':           round(vl_h, 0),
                'nav_indice':        snapshot['nav_indice'],
                'brvm30_official':   snapshot.get('brvm30_official'),
                'perf_since_launch': snapshot.get('perf_since_launch'),
                'change_1d_pct':     snapshot.get('change_1d_pct'),
                'change_day_pct':    snapshot.get('change_day_pct'),
                'aum_mfcfa':         snapshot.get('aum_mfcfa'),
                'n_prices':          snapshot.get('n_prices'),
            })
        with open(hist_path, 'w', encoding='utf-8') as fh:
            json.dump(hist, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

    # Push vers GitHub pour que le dashboard cloud soit a jour
    try:
        import subprocess
        push_bat = os.path.join(BASE_DIR, 'push_data.bat')
        if os.path.exists(push_bat):
            subprocess.Popen([push_bat], creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass

    vl_display = vl_live if vl_live else nav_result['vl_par_part_fcfa']
    launch_str = f" | lancement {perf_launch:+.3f}%" if perf_launch is not None else ""
    _log(f"iNAV={nav_result['nav_indice']:.4f} VL={vl_display:,.0f} FCFA jour={change_day:+.3f}%{launch_str}")
    print(f"[{snapshot['time']}] iNAV {nav_result['nav_indice']:.2f} | "
          f"VL {vl_display:,.0f} FCFA | jour {change_day:+.3f}%{launch_str}")

    return snapshot


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='iNAV CGF BRVM30 ETF — snapshot 15min')
    parser.add_argument('--force', action='store_true',
                        help='Forcer même hors heures de marché')
    args = parser.parse_args()

    result = run(force=args.force)
    if result is None and not args.force:
        print("Hors heures de marche BRVM (09h00–15h30 UTC). Utilisez --force pour tester.")
