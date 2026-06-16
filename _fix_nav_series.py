"""
Ajoute les VL journalieres manquantes dans nav_latest.json nav_live_series
en utilisant le dernier snapshot intraday de chaque jour.
"""
import json, os

BASE = r'c:\Users\l.philippe\OneDrive - CGF BOURSE\Bureau\test_BRVM30'

# Charger historique intraday
with open(os.path.join(BASE, 'nav_intraday_history.json'), encoding='utf-8') as f:
    hist = json.load(f)

# Charger nav_latest
with open(os.path.join(BASE, 'nav_latest.json'), encoding='utf-8') as f:
    nl = json.load(f)

series = nl.get('nav_live_series', [])
existing_dates = {d for d, v in series}
print('Dates deja dans nav_live_series:', sorted(existing_dates)[-5:])

# VL de cloture de chaque jour depuis l'historique intraday
added = 0
for day in sorted(hist):
    if day in existing_dates:
        continue
    pts = hist[day]
    if not pts:
        continue
    last = pts[-1]
    vl = last.get('vl_fcfa') or last.get('vl')
    if vl:
        series.append([day, round(float(vl), 2)])
        print(f'  Ajoute {day}: VL={vl} (snapshot {last["time"]})')
        added += 1

if added:
    series.sort(key=lambda x: x[0])
    nl['nav_live_series'] = series
    with open(os.path.join(BASE, 'nav_latest.json'), 'w', encoding='utf-8') as f:
        json.dump(nl, f, ensure_ascii=False, indent=2)
    print(f'nav_latest.json mis a jour ({added} dates ajoutees)')
else:
    print('Rien a ajouter')
