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
import numpy as np
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from base import BaseScript

# ── Paramètres de sélection (identiques au backtest) ─────────────────────────
FORCE_WEIGHT        = 0.03     # ≥ 3% poids indice → forcé quoi qu'il arrive
STALE_THRESH        = 0.70     # ≥ 70% jours sans cotation → exclu (sauf forcé)
STALE_WINDOW        = 63       # fenêtre 3 mois (jours ouvrés)
MAX_EXEC_NEW_DAYS   = 100      # nouveau entrant : max 100j d'exécution
MAX_EXEC_EXIST_DAYS = 32       # titre existant  : max 32j
CONSEC_REBALS_EXIT  = 2        # 2 rebals consécutifs > seuil → sortie
MIN_BASKET_WEIGHT   = 0.001    # < 0.1% après redistribution → exclu

# Liste manuelle des titres exclus pour float < 7 Md FCFA
# À mettre à jour si de nouveaux titres à petit flottant entrent dans le BRVM30
FLOAT_EXCLUDE = {"SEMC", "SIVC", "NEIC"}


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
            return None, [], {}
        last = rebals[-1]
        # Récupère le compteur excess_days du dernier rebal
        excess = last.get("excess_days_cnt", {})
        return last["date"], last["basket"], excess

    def _compute_adv(self, ticker: str, as_of_date: str, sika: dict) -> float:
        hist  = sika.get(ticker, {})
        dates = sorted(d for d in hist if d < as_of_date)[-STALE_WINDOW:]
        # Dénominateur = tous les jours de la fenêtre (y compris jours sans volume)
        vals  = [(hist[d].get("volume") or 0) * (hist[d].get("close") or 0) / 1e6
                 for d in dates]
        return float(sum(vals) / len(dates)) if dates else 0.0

    def _compute_stale(self, ticker: str, as_of_date: str, sika: dict) -> float:
        hist  = sika.get(ticker, {})
        dates = sorted(d for d in hist if d < as_of_date)[-STALE_WINDOW:]
        if not dates:
            return 1.0
        return sum(1 for d in dates if (hist[d].get("volume") or 0) == 0) / len(dates)

    def _estimate_weights(self, tickers: list, sika: dict) -> dict:
        """Estimation des poids BRVM30 depuis les prix (proxy market cap)."""
        prices = {}
        for tk in tickers:
            hist = sika.get(tk, {})
            if hist:
                latest = max(hist.keys())
                p = hist[latest].get("close") or hist[latest].get("close_adj")
                if p:
                    prices[tk] = float(p)
        n = len(tickers)
        total = sum(prices.get(tk, 0) for tk in tickers if prices.get(tk))
        weights = {}
        for tk in tickers:
            if tk in prices and total > 0:
                weights[tk] = round(prices[tk] / total, 6)
            else:
                weights[tk] = round(1 / n, 6)
        return weights

    def _apply_selection_rules(
        self,
        tickers: list,
        w_brvm30: dict,
        as_of_date: str,
        prev_basket: set,
        excess_days_cnt: dict,
        sika: dict,
        aum_mfcfa: float,
    ) -> tuple:
        """
        Applique les 5 règles de sélection.
        Retourne (basket_weights, excluded_list, new_excess_days_cnt).
        basket_weights : {ticker: w_etf}
        excluded_list  : [{"ticker", "w_brvm30", "raison", "adv_mfcfa", "stale_ratio",
                           "trade_mfcfa", "days_exec"}]
        """
        forced, included, excluded = [], [], []
        new_excess = dict(excess_days_cnt)

        for tk in tickers:
            w_b30   = w_brvm30.get(tk, 1 / max(len(tickers), 1))
            adv     = self._compute_adv(tk, as_of_date, sika)
            stale   = self._compute_stale(tk, as_of_date, sika)
            trade   = w_b30 * aum_mfcfa
            exec_d  = trade / adv if adv > 0 else 999.0
            is_new  = tk not in prev_basket

            # Float
            if tk in FLOAT_EXCLUDE:
                excluded.append({
                    "ticker": tk, "w_brvm30": round(w_b30, 6),
                    "raison": "Float < 7 Md FCFA",
                    "adv_mfcfa": round(adv, 1), "stale_ratio": round(stale, 3),
                    "trade_mfcfa": round(trade, 1), "days_exec": round(exec_d, 1),
                })
                new_excess.pop(tk, None)
                continue

            # Force ≥ 3%
            if w_b30 >= FORCE_WEIGHT:
                forced.append((tk, w_b30, adv, stale, trade, exec_d))
                new_excess.pop(tk, None)
                continue

            # Stale ≥ 70%
            if stale >= STALE_THRESH:
                excluded.append({
                    "ticker": tk, "w_brvm30": round(w_b30, 6),
                    "raison": f"Stale {stale*100:.0f}% (3 mois)",
                    "adv_mfcfa": round(adv, 1), "stale_ratio": round(stale, 3),
                    "trade_mfcfa": round(trade, 1), "days_exec": round(exec_d, 1),
                })
                new_excess.pop(tk, None)
                continue

            # ADV insuffisant
            max_days = MAX_EXEC_NEW_DAYS if is_new else MAX_EXEC_EXIST_DAYS
            if exec_d > max_days:
                if is_new:
                    excluded.append({
                        "ticker": tk, "w_brvm30": round(w_b30, 6),
                        "raison": f"ADV insuffisant nouveau entrant : {exec_d:.0f}j > {max_days}j",
                        "adv_mfcfa": round(adv, 1), "stale_ratio": round(stale, 3),
                        "trade_mfcfa": round(trade, 1), "days_exec": round(exec_d, 1),
                    })
                    new_excess.pop(tk, None)
                else:
                    new_excess[tk] = new_excess.get(tk, 0) + 1
                    if new_excess[tk] >= CONSEC_REBALS_EXIT:
                        excluded.append({
                            "ticker": tk, "w_brvm30": round(w_b30, 6),
                            "raison": f"ADV insuffisant {CONSEC_REBALS_EXIT} rebals consécutifs : {exec_d:.0f}j > {max_days}j",
                            "adv_mfcfa": round(adv, 1), "stale_ratio": round(stale, 3),
                            "trade_mfcfa": round(trade, 1), "days_exec": round(exec_d, 1),
                        })
                        new_excess.pop(tk, None)
                    else:
                        included.append((tk, w_b30, adv, stale, trade, exec_d))
            else:
                new_excess.pop(tk, None)
                included.append((tk, w_b30, adv, stale, trade, exec_d))

        # Assembler panier brut
        basket_raw = [(tk, w, adv, stale, trade, exec_d)
                      for tk, w, adv, stale, trade, exec_d in forced + included]

        # Règle 5 : poids minimum 0.1%
        total_w = sum(w for _, w, *_ in basket_raw)
        final = []
        for tk, w_b30, adv, stale, trade, exec_d in basket_raw:
            w_norm = w_b30 / total_w if total_w > 0 else 1 / max(len(basket_raw), 1)
            if w_norm < MIN_BASKET_WEIGHT and w_b30 < FORCE_WEIGHT:
                excluded.append({
                    "ticker": tk, "w_brvm30": round(w_b30, 6),
                    "raison": f"Poids < {MIN_BASKET_WEIGHT*100:.1f}% après redistribution",
                    "adv_mfcfa": round(adv, 1), "stale_ratio": round(stale, 3),
                    "trade_mfcfa": round(trade, 1), "days_exec": round(exec_d, 1),
                })
            else:
                final.append((tk, w_b30, adv, stale, trade, exec_d))

        total_f = sum(w for _, w, *_ in final)
        basket_weights = {}
        basket_detail  = []
        for tk, w_b30, adv, stale, trade, exec_d in final:
            w_etf = round(w_b30 / total_f, 6) if total_f > 0 else round(1 / len(final), 6)
            basket_weights[tk] = w_etf
            basket_detail.append({
                "ticker":     tk,
                "w_etf":      w_etf,
                "w_brvm30":   round(w_b30, 6),
                "force":      w_b30 >= FORCE_WEIGHT,
                "adv_mfcfa":  round(adv, 1),
                "stale_ratio": round(stale, 3),
                "trade_mfcfa": round(w_etf * aum_mfcfa, 1),
                "days_exec":  round((w_etf * aum_mfcfa) / adv if adv > 0 else 999, 1),
            })

        return basket_weights, basket_detail, excluded, new_excess

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
        excluded = proposal.get("excluded", [])
        rd       = proposal.get("proposed_rebal_date", "?")
        turnover = proposal.get("turnover_pct", "?")
        n_basket = len(proposal.get("new_basket", []))

        excl_lines = "\n".join(
            f"    {e['ticker']:8s} ({e['w_brvm30']*100:.2f}%) — {e['raison']}"
            for e in excluded
        ) or "    aucun"

        body = (
            f"Bonjour,\n\n"
            f"Un nouveau rebalancement du BRVM30 ETF est proposé pour le {rd}.\n\n"
            f"CHANGEMENTS DE COMPOSITION :\n"
            f"  → Entrants ({len(entries)}) : {', '.join(entries) or 'aucun'}\n"
            f"  → Sortants ({len(exits)})   : {', '.join(exits) or 'aucun'}\n\n"
            f"RÈGLES DE SÉLECTION APPLIQUÉES :\n"
            f"  → Panier ETF : {n_basket} titres\n"
            f"  → Exclusions ({len(excluded)}) :\n{excl_lines}\n\n"
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
        last_rebal_date, current_basket, excess_days_cnt = self._load_current_basket()

        new_comp       = self.load_json("brvm_composition_latest.json", {})
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

        sika        = self.load_json("sika_history.json", {})
        new_tickers = new_comp.get("composition", [])
        entries     = new_comp.get("entries", [])
        exits       = new_comp.get("exits",   [])
        prev_basket = {b["ticker"] for b in current_basket}

        # Lire l'AUM actuel depuis nav_latest
        nav_latest  = self.load_json("nav_latest.json", {})
        aum_mfcfa   = nav_latest.get("aum_mfcfa") or 5000.0

        # Estimer les poids BRVM30 depuis les prix
        w_brvm30 = self._estimate_weights(new_tickers, sika)

        # Appliquer les règles de sélection
        basket_weights, basket_detail, excluded, new_excess = self._apply_selection_rules(
            tickers=new_tickers,
            w_brvm30=w_brvm30,
            as_of_date=new_rebal_date,
            prev_basket=prev_basket,
            excess_days_cnt=excess_days_cnt,
            sika=sika,
            aum_mfcfa=aum_mfcfa,
        )

        turnover = self._compute_turnover(current_basket, basket_weights)

        proposal = {
            "status":              "pending",
            "proposed_at":         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "proposed_rebal_date": new_rebal_date,
            "last_rebal_date":     last_rebal_date,
            "entries":             entries,
            "exits":               exits,
            "turnover_pct":        turnover,
            "aum_mfcfa":           aum_mfcfa,
            "new_basket":          basket_detail,
            "excluded":            excluded,
            "excess_days_cnt":     new_excess,
            "current_basket": [
                {"ticker": b["ticker"], "w_etf": b.get("w_etf", 0.0)}
                for b in current_basket
            ],
        }

        self.save_json("rebal_pending.json", proposal)
        print(f"[OK] Proposition sauvegardée dans rebal_pending.json")
        print(f"  Entrants : {entries}")
        print(f"  Sortants : {exits}")
        print(f"  Panier ETF : {len(basket_detail)} titres")
        print(f"  Exclus ({len(excluded)}) :")
        for e in excluded:
            print(f"    {e['ticker']:8s} ({e['w_brvm30']*100:.2f}%) — {e['raison']}")
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
