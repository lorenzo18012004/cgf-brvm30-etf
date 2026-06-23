"""
update_brvm30_history.py — Mise à jour quotidienne de l'historique BRVM30 officiel
Extrait la valeur de clôture BRVM30 depuis les snapshots iNAV et scrape Sika en fallback.
Usage : python update_brvm30_history.py
"""

import os, sys, json
from datetime import date, datetime, timezone

import requests
from bs4 import BeautifulSoup

from base import BaseScript


class Brvm30HistoryUpdater(BaseScript):

    def _load(self, fname):
        path = os.path.join(self.data_dir, fname)
        if not os.path.exists(path):
            return None
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def _save(self, fname, data):
        path = os.path.join(self.data_dir, fname)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _scrape_brvm30_from_sika(self):
        """Récupère la valeur actuelle du BRVM30 officiel depuis Sika Finance."""
        try:
            resp = requests.get(
                'https://sikafinance.com/marches/aaz',
                headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'fr-FR'},
                verify=False, timeout=15,
            )
            soup = BeautifulSoup(resp.text, 'html.parser')
            # Chercher la valeur de l'indice BRVM30 dans la page
            for tag in soup.find_all(string=lambda t: t and 'BRVM30' in t):
                parent = tag.find_parent()
                if not parent:
                    continue
                row = parent.find_parent('tr')
                if not row:
                    continue
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    for cell in cells[1:5]:
                        txt = cell.get_text(strip=True).replace('\xa0', '').replace(' ', '').replace(',', '.')
                        try:
                            val = float(txt)
                            if 100 < val < 10000:
                                print(f"  [Sika fallback] BRVM30 = {val}")
                                return val
                        except ValueError:
                            continue
        except Exception as e:
            print(f"  [Sika fallback] Erreur scraping : {e}")
        return None

    def run(self):
        ih   = self._load('nav_intraday_history.json') or {}
        brvm = self._load('brvm30_index_history.json') or {}

        today_str  = date.today().isoformat()
        now_utc_h  = datetime.now(timezone.utc).hour
        market_closed = now_utc_h >= 16  # BRVM ferme à 15h30 UTC

        updated = 0

        for day, snaps in sorted(ih.items()):
            if not snaps:
                continue

            # Ne jamais écraser une valeur déjà enregistrée pour un jour passé
            if day in brvm and day < today_str:
                continue

            # Prendre la DERNIÈRE valeur brvm30_official du jour (= clôture)
            val = None
            for snap in reversed(snaps):
                v = snap.get('brvm30_official')
                if v:
                    val = float(v)
                    break

            if val is None:
                continue

            # Pour today : n'enregistrer qu'après la clôture du marché
            if day == today_str and not market_closed:
                print(f"  {day} : marché encore ouvert, skip.")
                continue

            if brvm.get(day) != val:
                brvm[day] = val
                updated += 1
                print(f"  {day} : {val}")

        # Fallback Sika pour today si toujours manquant après clôture
        if today_str not in brvm and market_closed:
            print(f"  {today_str} manquant après clôture — tentative scraping Sika...")
            sika_val = self._scrape_brvm30_from_sika()
            if sika_val:
                brvm[today_str] = sika_val
                updated += 1
                print(f"  {today_str} : {sika_val} (via Sika)")

        brvm_sorted = dict(sorted(brvm.items()))
        self._save('brvm30_index_history.json', brvm_sorted)
        print(f"brvm30_index_history.json mis à jour : {updated} entrée(s). Total : {len(brvm_sorted)} jours.")
        return brvm_sorted


if __name__ == '__main__':
    Brvm30HistoryUpdater().run()
