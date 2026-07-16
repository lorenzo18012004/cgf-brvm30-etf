"""
data_provider_template.py — Modèle pour connecter une nouvelle source de données BRVM
======================================================================================

INSTRUCTIONS POUR IT
---------------------
1. Copier ce fichier :  data_provider_<nom_source>.py   (ex: data_provider_bloomberg.py)
2. Renommer la classe :  TemplateProvider → <NomSource>Provider  (ex: BloombergProvider)
3. Implémenter les 4 méthodes (les NotImplementedError indiquent ce qui reste à faire)
4. Dans GitHub Actions → Settings → Secrets, ajouter :
       DATA_PROVIDER = <nom_source>      (ex: bloomberg)
5. Supprimer ce fichier template du repo une fois le vrai provider en place.

AUCUNE AUTRE MODIFICATION N'EST NÉCESSAIRE dans le reste du code.
Les scripts calc_nav_cloud.py, scrape_intraday_cloud.py, rebalance_live.py
utiliseront automatiquement le nouveau provider.
"""

import json
import os

import pandas as pd

from data_provider import BRVMDataProvider


# Tickers BRVM30 courants (juillet 2026) — mettre à jour si la composition change
BRVM30_TICKERS = [
    "BICB",  "BOAB",  "BOABF", "BOAC",  "BOAM",  "BOAN",  "BOAS",
    "CBIBF", "CFAC",  "CIEC",  "ECOC",  "ETIT",  "NEIC",  "NSBC",
    "NTLC",  "ORAC",  "ORGT",  "SAFC",  "SCRC",  "SDSC",  "SEMC",
    "SGBC",  "SIBC",  "SIVC",  "SNTS",  "SOGC",  "SPHC",  "STBC",
    "TTLC",  "UNXC",
]


class TemplateProvider(BRVMDataProvider):
    """
    Provider modèle — remplacer 'Template' par le nom de votre source.
    Exemple : BloombergProvider, BRVMApiProvider, ReutersProvider...
    """

    def __init__(self):
        self.data_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "data")
        )
        # Initialiser ici votre client API :
        #   api_key = os.environ["MON_API_KEY"]
        #   self.client = MonClientAPI(api_key=api_key)

    # ── 1. Cours en temps réel ──────────────────────────────────────────────

    def get_live_prices(self) -> pd.Series:
        """
        Récupérer les cours intraday ou de clôture depuis votre source.
        Appelé toutes les 5 minutes pendant les heures de marché (09h-15h30 UTC).

        DOIT retourner une Series pandas { ticker: float }.

        Exemple avec une API fictive :
            quotes = self.client.get_quotes(BRVM30_TICKERS)
            return pd.Series({q["ticker"]: float(q["last"]) for q in quotes})
        """
        raise NotImplementedError("Implémenter get_live_prices()")

    # ── 2. Valeur de l'indice BRVM30 ────────────────────────────────────────

    def get_brvm30_index(self) -> "float | None":
        """
        Valeur courante de l'indice BRVM30 (ex: 285.42).
        Retourner None si non disponible.

        Exemple :
            data = self.client.get_index("BRVM30")
            return float(data["value"]) if data else None
        """
        raise NotImplementedError("Implémenter get_brvm30_index()")

    # ── 3. Historique des prix de clôture ───────────────────────────────────

    def update_price_history(self, tickers: "list | None" = None) -> dict:
        """
        Télécharger les cours de clôture et mettre à jour data/sika_history.json.
        Appelé quotidiennement par GitHub Actions.

        FORMAT OBLIGATOIRE de sika_history.json :
        {
          "SNTS": {
            "2024-01-02": {"close": 15000.0, "volume": 1234, "open": 14900.0, "high": 15100.0, "low": 14800.0},
            "2024-01-03": {"close": 15200.0, "volume": 890,  "open": 15000.0, "high": 15300.0, "low": 14950.0}
          },
          "ETIT": { ... }
        }

        Notes :
          - "volume", "open", "high", "low" sont optionnels (seul "close" est utilisé par le moteur NAV)
          - Les dates doivent être au format ISO : "YYYY-MM-DD"
          - Fusionner avec les données existantes (ne pas écraser l'historique)

        Exemple d'implémentation :
            history_path = os.path.join(self.data_dir, "sika_history.json")
            existing = {}
            if os.path.exists(history_path):
                with open(history_path, encoding="utf-8") as f:
                    existing = json.load(f)

            for ticker in (tickers or BRVM30_TICKERS):
                raw = self.client.get_ohlcv(ticker, start="2005-01-01")
                existing.setdefault(ticker, {}).update({
                    item["date"]: {
                        "close":  float(item["close"]),
                        "volume": int(item.get("volume", 0)),
                        "open":   float(item.get("open",  item["close"])),
                        "high":   float(item.get("high",  item["close"])),
                        "low":    float(item.get("low",   item["close"])),
                    }
                    for item in raw
                })

            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)

            return {tk: len(existing.get(tk, {})) for tk in (tickers or BRVM30_TICKERS)}
        """
        raise NotImplementedError("Implémenter update_price_history()")

    # ── 4. Données sociétés (capitalisation) ────────────────────────────────

    def update_societe(self, tickers: "list | None" = None) -> dict:
        """
        Mettre à jour data/sika_societe.json (nb de titres, flottant, capitalisation).
        Appelé lors des rebalancements trimestriels.

        FORMAT OBLIGATOIRE de sika_societe.json :
        {
          "SNTS": {
            "nb_titres":          50000000.0,   ← nombre total de titres en circulation
            "flottant_pct":       25.0,          ← % du flottant (optionnel)
            "valorisation_mfcfa": 1500000.0      ← capitalisation en millions FCFA
          },
          ...
        }

        Note : le moteur de rebalancement utilise nb_titres × prix_clôture
        pour calculer les poids cibles (replication physique totale).

        Exemple d'implémentation :
            societe_path = os.path.join(self.data_dir, "sika_societe.json")
            existing = {}
            if os.path.exists(societe_path):
                with open(societe_path, encoding="utf-8") as f:
                    existing = json.load(f)

            for ticker in (tickers or BRVM30_TICKERS):
                info = self.client.get_company_info(ticker)
                existing[ticker] = {
                    "nb_titres":          float(info["shares_outstanding"]),
                    "flottant_pct":       float(info.get("float_pct", 100.0)),
                    "valorisation_mfcfa": float(info["market_cap"]) / 1_000_000,
                }

            with open(societe_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)

            return {tk: existing[tk]["nb_titres"] for tk in existing}
        """
        raise NotImplementedError("Implémenter update_societe()")
