"""
apply_rebalancing.py — Application d'un rebalancement après validation manuelle
================================================================================
Lit rebal_pending.json (généré par propose_rebalancing.py) et l'ajoute
à rebal_detail.json. Déclenché UNIQUEMENT via workflow_dispatch sur GitHub.

Usage :
    python apply_rebalancing.py
"""

import os, sys
from datetime import datetime, timezone

from base import BaseScript


class RebalancingApplier(BaseScript):

    def run(self) -> bool:
        pending = self.load_json("rebal_pending.json", {})

        if not pending:
            print("[ERREUR] rebal_pending.json vide ou absent.")
            return False

        status = pending.get("status")
        if status == "applied":
            print(f"[INFO] Ce rebalancement a déjà été appliqué le "
                  f"{pending.get('applied_at', '?')}.")
            return False
        if status != "pending":
            print(f"[ERREUR] Statut inattendu dans rebal_pending.json : {status!r}")
            return False

        rebal_date  = pending["proposed_rebal_date"]
        new_basket  = pending["new_basket"]
        entries     = pending.get("entries", [])
        exits       = pending.get("exits",   [])
        turnover    = pending.get("turnover_pct", 0.0)

        print(f"[OK] Application du rebalancement {rebal_date}...")
        print(f"  Entrants : {entries or 'aucun'}")
        print(f"  Sortants : {exits or 'aucun'}")
        print(f"  Turnover estimé : {turnover}%")
        print(f"  Tickers : {[b['ticker'] for b in new_basket]}")

        rd = self.load_json("rebal_detail.json", {"rebalancings": []})

        # Vérifier que ce rebalancement n'existe pas déjà
        existing_dates = [r["date"] for r in rd.get("rebalancings", [])]
        if rebal_date in existing_dates:
            print(f"[WARN] Rebalancement {rebal_date} déjà présent dans rebal_detail.json.")
            pending["status"]     = "applied"
            pending["applied_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            self.save_json("rebal_pending.json", pending)
            return False

        new_entry = {
            "date":       rebal_date,
            "date_label": rebal_date,
            "skipped":    False,
            "basket_n":   len(new_basket),
            "excl_n":     0,
            "excl_w":     0.0,
            "turnover":   round(turnover / 100, 4),
            "entries":    entries,
            "exits":      exits,
            "basket": [
                {
                    "ticker":   b["ticker"],
                    "w_etf":    b["w_etf"],
                    "w_brvm30": b["w_etf"],
                    "delta":    0.0,
                    "source":   "auto_proposal",
                }
                for b in new_basket
            ],
            "excluded":   [],
            "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "source":     "auto_proposal",
        }

        rd["rebalancings"].append(new_entry)
        self.save_json("rebal_detail.json", rd)

        pending["status"]     = "applied"
        pending["applied_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        self.save_json("rebal_pending.json", pending)

        print(f"[OK] Rebalancement {rebal_date} ajouté à rebal_detail.json.")
        print("[OK] rebal_pending.json marqué comme appliqué.")
        return True


if __name__ == "__main__":
    RebalancingApplier().run()
