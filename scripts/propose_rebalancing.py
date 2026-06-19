"""
propose_rebalancing.py — Détection automatique de nouveau rebalancement BRVM30
==============================================================================
Appelé mensuellement par GitHub Actions.
Compare brvm_composition_latest.json avec le dernier rebal dans rebal_detail.json.
Si une nouvelle composition existe → génère rebal_pending.json + envoie un email.
Rien n'est appliqué : la validation est manuelle (workflow apply_rebalancing).

Usage :
    python propose_rebalancing.py           # vérification normale
    python propose_rebalancing.py --force   # forcer même si déjà proposé
"""

import os, sys, json, smtplib, argparse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from base import BaseScript


class RebalancingProposer(BaseScript):

    def __init__(self):
        super().__init__()
        self.recipient = "l.philippe@cgfgestion.com"

    # ------------------------------------------------------------------ #

    def _load_current_basket(self) -> tuple:
        rd = self.load_json("rebal_detail.json", {"rebalancings": []})
        rebals = [r for r in rd.get("rebalancings", [])
                  if not r.get("skipped") and r.get("basket")]
        if not rebals:
            return None, []
        last = rebals[-1]
        return last["date"], last["basket"]

    def _estimate_weights(self, tickers: list) -> dict:
        sika = self.load_json("sika_history.json", {})
        prices = {}
        for tk in tickers:
            hist = sika.get(tk, {})
            if hist:
                latest = max(hist.keys())
                p = hist[latest].get("close") or hist[latest].get("close_adj")
                if p:
                    prices[tk] = float(p)

        n = len(tickers)
        if not prices:
            return {tk: round(1 / n, 6) for tk in tickers}

        total = sum(prices.get(tk, 0) for tk in tickers if prices.get(tk))
        weights = {}
        for tk in tickers:
            if tk in prices and total > 0:
                weights[tk] = round(prices[tk] / total, 6)
            else:
                weights[tk] = round(1 / n, 6)
        return weights

    def _compute_turnover(self, old_basket: list, new_weights: dict) -> float:
        old_w = {b["ticker"]: b.get("w_etf", 0.0) for b in old_basket}
        all_tickers = set(old_w) | set(new_weights)
        tv = sum(abs(new_weights.get(tk, 0.0) - old_w.get(tk, 0.0))
                 for tk in all_tickers)
        return round(tv / 2 * 100, 1)

    def _send_email(self, proposal: dict):
        gmail_user = os.environ.get("GMAIL_USER")
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
        if not gmail_user:
            secrets = self.load_json_path(
                os.path.join(self.root_dir, "secrets.json")) or {}
            gmail_user = secrets.get("smtp_user")
            gmail_pass = secrets.get("smtp_pass")
        if not gmail_user:
            print("[WARN] Pas de credentials email — notification ignorée.")
            return

        entries  = proposal.get("entries", [])
        exits    = proposal.get("exits",   [])
        rd       = proposal.get("proposed_rebal_date", "?")
        turnover = proposal.get("turnover_pct", "?")

        body = (
            f"Bonjour,\n\n"
            f"Un nouveau rebalancement du BRVM30 ETF est proposé pour le {rd}.\n\n"
            f"CHANGEMENTS DE COMPOSITION :\n"
            f"  → Entrants ({len(entries)}) : {', '.join(entries) or 'aucun'}\n"
            f"  → Sortants ({len(exits)})   : {', '.join(exits) or 'aucun'}\n\n"
            f"Turnover estimé : {turnover}%\n\n"
            f"PROCHAINE ÉTAPE :\n"
            f"GitHub → Actions → 'Appliquer Rebalancement' → Run workflow\n"
            f"pour valider et appliquer ce rebalancement.\n\n"
            f"Rien ne sera modifié sans votre confirmation explicite.\n\n"
            f"Cordialement,\n"
            f"CGF Bourse — Système automatique"
        )

        msg = MIMEMultipart()
        msg["From"]    = gmail_user
        msg["To"]      = self.recipient
        msg["Subject"] = f"[CGF BRVM30 ETF] Rebalancement proposé — {rd}"
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, self.recipient, msg.as_string())
        print(f"[OK] Email de proposition envoyé à {self.recipient}")

    # ------------------------------------------------------------------ #

    def run(self, force: bool = False):
        last_rebal_date, current_basket = self._load_current_basket()

        new_comp      = self.load_json("brvm_composition_latest.json", {})
        new_rebal_date = new_comp.get("rebal_date")

        if not new_rebal_date:
            print("[INFO] Pas de composition BRVM30 disponible — rien à faire.")
            return

        if not force and last_rebal_date and new_rebal_date <= last_rebal_date:
            print(f"[INFO] Composition {new_rebal_date} déjà appliquée "
                  f"(dernier rebal : {last_rebal_date}). Rien à faire.")
            return

        pending = self.load_json("rebal_pending.json", {})
        if (not force
                and pending.get("status") == "pending"
                and pending.get("proposed_rebal_date") == new_rebal_date):
            print(f"[INFO] Proposition pour {new_rebal_date} déjà en attente.")
            return

        print(f"[INFO] Nouvelle composition BRVM30 détectée ({new_rebal_date}).")

        new_tickers = new_comp.get("composition", [])
        entries     = new_comp.get("entries", [])
        exits       = new_comp.get("exits",   [])
        new_weights = self._estimate_weights(new_tickers)
        turnover    = self._compute_turnover(current_basket, new_weights)

        proposal = {
            "status":              "pending",
            "proposed_at":         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "proposed_rebal_date": new_rebal_date,
            "last_rebal_date":     last_rebal_date,
            "entries":             entries,
            "exits":               exits,
            "turnover_pct":        turnover,
            "new_basket": [
                {"ticker": tk, "w_etf": new_weights.get(tk, round(1 / len(new_tickers), 6))}
                for tk in new_tickers
            ],
            "current_basket": [
                {"ticker": b["ticker"], "w_etf": b.get("w_etf", 0.0)}
                for b in current_basket
            ],
        }

        self.save_json("rebal_pending.json", proposal)
        print(f"[OK] Proposition sauvegardée dans rebal_pending.json")
        print(f"  Entrants : {entries}")
        print(f"  Sortants : {exits}")
        print(f"  Turnover estimé : {turnover}%")

        try:
            self._send_email(proposal)
        except Exception as e:
            print(f"[WARN] Email non envoyé : {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Proposition de rebalancement BRVM30")
    parser.add_argument("--force", action="store_true",
                        help="Forcer même si déjà proposé")
    args = parser.parse_args()
    RebalancingProposer().run(force=args.force)
