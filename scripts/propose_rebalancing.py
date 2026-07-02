"""
propose_rebalancing.py — Détection automatique de nouveau rebalancement BRVM30
==============================================================================
Appelé mensuellement par GitHub Actions.
Compare brvm_composition_latest.json avec le dernier rebal dans rebal_detail.json.
Si une nouvelle composition existe → génère rebal_pending.json + envoie un email.

Stratégie identique à rebalance_live.py :
  - Poids cible : capitalisation totale Sika (nb_titres × prix)
  - Top FORCE_TOP_N forcés (OTC, sans contrainte ADV)
  - Restants : ADV-cap (participation 15% × 62j/32j) + redistribution
  - Exclusion uniquement si ADV < MIN_ADV_MFCFA ou poids < 0.1% après redistribution
  - Pas d'exclusion float fixe

Usage :
    python propose_rebalancing.py           # vérification normale
    python propose_rebalancing.py --force   # forcer même si déjà proposé
"""

import os, sys, json, smtplib, argparse
from datetime import datetime, timezone

from base import BaseScript

# ── Paramètres (identiques à rebalance_live.py) ──────────────────────────────
MAX_EXEC_LARGE   = 62
MAX_EXEC_SMALL   = 32
LARGE_THRESHOLD  = 0.03
PARTICIPATION_RATE = 0.15
MIN_ADV_MFCFA    = 0.5
MIN_WEIGHT       = 0.001
STALE_WINDOW     = 63
FORCE_TOP_N      = 5


class RebalancingProposer(BaseScript):

    def __init__(self):
        super().__init__()
        self.recipient = "l.philippe@cgfgestion.com"

    # ── Helpers Sika ──────────────────────────────────────────────────────── #

    def _last_price(self, sika, ticker, as_of_date):
        hist = sika.get(ticker, {})
        past = sorted(d for d in hist if d <= as_of_date)
        if past:
            p = hist[past[-1]]
            close = p.get("close") if isinstance(p, dict) else p
            if close and float(close) > 0:
                return float(close)
        return None

    def _compute_adv(self, sika, ticker, as_of_date):
        hist  = sika.get(ticker, {})
        dates = sorted(d for d in hist if d < as_of_date)[-STALE_WINDOW:]
        vals  = [(hist[d].get("volume") or 0) * (hist[d].get("close") or 0) / 1e6
                 for d in dates]
        return float(sum(vals) / len(dates)) if dates else 0.0

    def _compute_stale(self, sika, ticker, as_of_date):
        hist  = sika.get(ticker, {})
        dates = sorted(d for d in hist if d < as_of_date)[-STALE_WINDOW:]
        if not dates:
            return 1.0
        return sum(1 for d in dates if (hist[d].get("volume") or 0) == 0) / len(dates)

    # ── Poids capitalisation totale Sika ──────────────────────────────────── #

    def _get_total_cap_weights(self, tickers, rebal_date, sika, soc):
        market_cap = {}
        missing    = []
        for tk in tickers:
            nb   = soc.get(tk, {}).get("nb_titres")
            prix = self._last_price(sika, tk, rebal_date)
            if nb and prix:
                market_cap[tk] = nb * prix
            else:
                missing.append(tk)
        if missing and market_cap:
            avg = sum(market_cap.values()) / len(market_cap)
            for tk in missing:
                market_cap[tk] = avg
        total = sum(market_cap.values())
        if total <= 0:
            return {tk: 1 / len(tickers) for tk in tickers}
        return {tk: market_cap[tk] / total for tk in tickers}

    # ── Stratégie ADV-cap (= rebalance_live.py) ───────────────────────────── #

    def _build_adv_capped_weights(self, w_brvm30, rebal_date, aum_mfcfa, sika):
        """
        Top FORCE_TOP_N titres forcés (OTC).
        Restants : ADV-cap participation 15% × 62j (grands) / 32j (petits).
        Exclusion uniquement si ADV < MIN_ADV_MFCFA ou poids résiduel < MIN_WEIGHT.
        """
        total_brvm30 = sum(w_brvm30.values()) or 1.0
        w_norm = {tk: v / total_brvm30 for tk, v in w_brvm30.items()}
        adv    = {tk: self._compute_adv(sika, tk, rebal_date) for tk in w_norm}

        sorted_tks = sorted(w_norm, key=lambda x: -w_norm[x])
        forced_tks = set(sorted_tks[:FORCE_TOP_N])
        rest_tks   = [tk for tk in sorted_tks if tk not in forced_tks]

        forced_w     = {tk: w_norm[tk] for tk in forced_tks}
        forced_total = sum(forced_w.values())
        rest_budget  = 1.0 - forced_total

        eligible = [tk for tk in rest_tks if adv[tk] >= MIN_ADV_MFCFA]
        exclu    = {tk: f"ADV {adv[tk]:.1f} MFCFA < {MIN_ADV_MFCFA}" for tk in rest_tks if adv[tk] < MIN_ADV_MFCFA}

        if not eligible:
            return {tk: round(v, 6) for tk, v in forced_w.items()}, exclu, forced_tks

        total_rest = sum(w_norm[tk] for tk in eligible) or 1.0
        weights    = {tk: w_norm[tk] / total_rest * rest_budget for tk in eligible}

        max_w = {}
        for tk in eligible:
            days    = MAX_EXEC_LARGE if w_norm[tk] >= LARGE_THRESHOLD else MAX_EXEC_SMALL
            max_w[tk] = min(PARTICIPATION_RATE * adv[tk] * days / aum_mfcfa, rest_budget)

        for _ in range(50):
            capped   = {tk for tk in eligible if weights[tk] > max_w[tk]}
            uncapped = [tk for tk in eligible if tk not in capped]
            if not capped:
                break
            excess = sum(weights[tk] - max_w[tk] for tk in capped)
            for tk in capped:
                weights[tk] = max_w[tk]
            uncapped_total = sum(weights[tk] for tk in uncapped)
            if uncapped_total <= 0 or not uncapped:
                break
            for tk in uncapped:
                weights[tk] += excess * weights[tk] / uncapped_total

        for _ in range(10):
            tiny = [tk for tk in eligible if 0 < weights.get(tk, 0) < MIN_WEIGHT]
            if not tiny:
                break
            for tk in tiny:
                exclu[tk] = f"Poids < {MIN_WEIGHT*100:.1f}% après redistribution"
                eligible.remove(tk)
            if not eligible:
                break
            total_keep = sum(weights[tk] for tk in eligible)
            for tk in eligible:
                weights[tk] = weights[tk] / total_keep * rest_budget if total_keep > 0 else rest_budget / len(eligible)

        final = {**forced_w, **{tk: weights[tk] for tk in eligible if weights.get(tk, 0) > 0}}
        total = sum(final.values())
        if total > 0:
            final = {tk: round(v / total, 6) for tk, v in final.items()}

        return final, exclu, forced_tks

    # ── Turnover ──────────────────────────────────────────────────────────── #

    def _compute_turnover(self, old_basket, new_weights):
        old_w = {b["ticker"]: b.get("w_etf", 0.0) for b in old_basket}
        all_tickers = set(old_w) | set(new_weights)
        tv = sum(abs(new_weights.get(tk, 0.0) - old_w.get(tk, 0.0)) for tk in all_tickers)
        return round(tv / 2 * 100, 1)

    # ── Email ─────────────────────────────────────────────────────────────── #

    def _send_email(self, proposal):
        gmail_user = os.environ.get("GMAIL_USER")
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
        if not gmail_user:
            secrets = self.load_json_path(os.path.join(self.root_dir, "secrets.json")) or {}
            gmail_user = secrets.get("smtp_user")
            gmail_pass = secrets.get("smtp_pass")
        if not gmail_user:
            print("[WARN] Pas de credentials email — notification ignorée.")
            return

        entries  = proposal.get("entries", [])
        exits    = proposal.get("exits", [])
        capped   = proposal.get("capped_tickers", [])
        excluded = proposal.get("excluded", [])
        rd       = proposal.get("proposed_rebal_date", "?")
        turnover = proposal.get("turnover_pct", "?")
        n_basket = len(proposal.get("new_basket", []))

        excl_lines = "\n".join(
            f"    {tk:8s} — {raison}" for tk, raison in excluded.items()
        ) or "    aucun"

        cap_lines = "\n".join(
            f"    {tk}" for tk in capped
        ) or "    aucun"

        body = (
            f"Bonjour,\n\n"
            f"Un nouveau rebalancement du BRVM30 ETF est proposé pour le {rd}.\n\n"
            f"CHANGEMENTS DE COMPOSITION :\n"
            f"  → Entrants ({len(entries)}) : {', '.join(entries) or 'aucun'}\n"
            f"  → Sortants ({len(exits)})   : {', '.join(exits) or 'aucun'}\n\n"
            f"STRATÉGIE ADV-CAP :\n"
            f"  → Panier ETF : {n_basket} titres\n"
            f"  → Top {FORCE_TOP_N} tenus à leur poids exact (OTC)\n"
            f"  → Titres plafonnés par ADV ({len(capped)}) :\n{cap_lines}\n"
            f"  → Exclus uniquement si ADV < {MIN_ADV_MFCFA} MFCFA ou poids résiduel < {MIN_WEIGHT*100:.1f}% ({len(excluded)}) :\n{excl_lines}\n\n"
            f"Turnover estimé : {turnover}%\n\n"
            f"PROCHAINE ÉTAPE :\n"
            f"GitHub → Actions → 'Appliquer Rebalancement' → Run workflow\n\n"
            f"Cordialement,\nCGF Bourse — Système automatique"
        )

        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        msg = MIMEMultipart()
        msg["From"]    = gmail_user
        msg["To"]      = self.recipient
        msg["Subject"] = f"[CGF BRVM30 ETF] Rebalancement proposé — {rd}"
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, self.recipient, msg.as_string())
        print(f"[OK] Email envoyé à {self.recipient}")

    # ── Point d'entrée ────────────────────────────────────────────────────── #

    def run(self, force=False):
        # Dernier rebal appliqué
        rd_data    = self.load_json("rebal_detail.json", {"rebalancings": []})
        rebals     = [r for r in rd_data.get("rebalancings", []) if not r.get("skipped") and r.get("basket")]
        last       = rebals[-1] if rebals else {}
        last_date  = last.get("date", "")
        old_basket = last.get("basket", [])

        # Nouvelle composition
        new_comp       = self.load_json("brvm_composition_latest.json", {})
        new_rebal_date = new_comp.get("rebal_date")

        if not new_rebal_date:
            print("[INFO] Pas de composition BRVM30 disponible — rien à faire.")
            return

        if not force and last_date and new_rebal_date <= last_date:
            print(f"[INFO] Composition {new_rebal_date} déjà appliquée (dernier rebal : {last_date}).")
            return

        pending = self.load_json("rebal_pending.json", {})
        if (not force
                and pending.get("status") == "pending"
                and pending.get("proposed_rebal_date") == new_rebal_date):
            print(f"[INFO] Proposition pour {new_rebal_date} déjà en attente.")
            return

        print(f"[INFO] Nouvelle composition BRVM30 détectée ({new_rebal_date}).")

        sika        = self.load_json("sika_history.json", {})
        soc         = self.load_json("sika_societe.json", {})
        new_tickers = new_comp.get("composition", [])
        entries     = new_comp.get("entries", [])
        exits       = new_comp.get("exits", [])

        nav_latest = self.load_json("nav_latest.json", {})
        aum_mfcfa  = float(nav_latest.get("aum_mfcfa") or 5000.0)

        # Poids capitalisation totale Sika
        w_brvm30 = self._get_total_cap_weights(new_tickers, new_rebal_date, sika, soc)

        # ADV-cap (même stratégie que rebalance_live.py)
        basket_weights, exclu_info, forced_tks = self._build_adv_capped_weights(
            w_brvm30, new_rebal_date, aum_mfcfa, sika
        )

        # Titres effectivement plafonnés (dans le panier mais w_etf < w_brvm30)
        capped_tickers = sorted(
            tk for tk in basket_weights
            if tk not in forced_tks and basket_weights[tk] < w_brvm30.get(tk, 0) - 1e-6
        )

        turnover = self._compute_turnover(old_basket, basket_weights)

        basket_detail = [
            {
                "ticker":      tk,
                "w_etf":       round(w, 6),
                "w_brvm30":    round(w_brvm30.get(tk, 0), 6),
                "force_otc":   tk in forced_tks,
                "capped":      tk in capped_tickers,
                "adv_mfcfa":   round(self._compute_adv(sika, tk, new_rebal_date), 1),
                "stale_ratio": round(self._compute_stale(sika, tk, new_rebal_date), 3),
            }
            for tk, w in sorted(basket_weights.items(), key=lambda x: -x[1])
        ]

        proposal = {
            "status":              "pending",
            "proposed_at":         datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "proposed_rebal_date": new_rebal_date,
            "last_rebal_date":     last_date,
            "entries":             entries,
            "exits":               exits,
            "turnover_pct":        turnover,
            "aum_mfcfa":           aum_mfcfa,
            "new_basket":          basket_detail,
            "capped_tickers":      capped_tickers,
            "excluded":            exclu_info,
            "current_basket":      [{"ticker": b["ticker"], "w_etf": b.get("w_etf", 0.0)} for b in old_basket],
        }

        self.save_json("rebal_pending.json", proposal)

        sorted_by_brvm = sorted(w_brvm30, key=lambda x: -w_brvm30[x])
        print(f"[OK] Proposition sauvegardée dans rebal_pending.json")
        print(f"  Entrants : {entries}")
        print(f"  Sortants : {exits}")
        print(f"  Top {FORCE_TOP_N} OTC : {', '.join(sorted_by_brvm[:FORCE_TOP_N])}")
        print(f"  Panier ETF : {len(basket_detail)} titres")
        if capped_tickers:
            print(f"  Plafonnés ADV ({len(capped_tickers)}) : {', '.join(capped_tickers)}")
        if exclu_info:
            print(f"  Exclus ({len(exclu_info)}) :")
            for tk, raison in exclu_info.items():
                print(f"    {tk:8s} ({w_brvm30.get(tk,0)*100:.2f}%) — {raison}")
        print(f"  Turnover estimé : {turnover}%")

        try:
            self._send_email(proposal)
        except Exception as e:
            print(f"[WARN] Email non envoyé : {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Forcer même si déjà proposé")
    args = parser.parse_args()
    RebalancingProposer().run(force=args.force)
