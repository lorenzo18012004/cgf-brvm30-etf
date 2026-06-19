"""
check_corporate_actions.py — Détection des actions corporatives BRVM
=====================================================================
Tourne à 9h00 UTC (avant ouverture marché) via GitHub Actions.

Détecte :
1. Anomalies de prix vs clôture de la veille (dividendes, splits non annoncés)
2. Événements connus dans dividend_calendar.json (rappel proactif)

Envoie un email d'alerte si quelque chose est détecté.
"""
import os, sys, smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from base import BaseScript


class CorporateActionsChecker(BaseScript):
    def __init__(self):
        super().__init__()
        self.THRESHOLD_DIV   = 0.04
        self.THRESHOLD_SPLIT = 0.15
        self.CALENDAR_DAYS   = 5
        self.RECIPIENT       = "l.philippe@cgfgestion.com"
        self.SIKA_HIST    = os.path.join(self.scripts_dir, "sika_history.json")
        self.NAV_LATEST   = os.path.join(self.scripts_dir, "nav_latest.json")
        self.DIV_CALENDAR = os.path.join(self.scripts_dir, "dividend_calendar.json")

    def _get_prev_close(self, sika_hist, ticker, today_str):
        hist = sika_hist.get(ticker, {})
        dates = sorted(d for d in hist if d < today_str)
        if not dates:
            return None
        return hist[dates[-1]].get("close")

    def _get_etf_weights(self, nav_latest):
        basket = (nav_latest or {}).get("basket", [])
        return {it["ticker"]: it["poids_pct"] for it in basket}

    def _scrape_live_prices(self):
        try:
            sys.path.insert(0, self.scripts_dir)
            from scrape_sika import _fetch_html, scrape_prices, SIKA_URL
            html = _fetch_html(SIKA_URL)
            prices = scrape_prices(html)
            return {tk: float(prices[tk]) for tk in prices.index}
        except Exception as e:
            print(f"[ERREUR] Scraping live : {e}")
            return {}

    def _check_calendar(self, today_str):
        cal = self.load_json_path(self.DIV_CALENDAR) or {}
        events = cal.get("events", [])
        today  = date.fromisoformat(today_str)
        upcoming = []
        for ev in events:
            try:
                ex = date.fromisoformat(ev["ex_date"])
                delta = (ex - today).days
                if 0 <= delta <= self.CALENDAR_DAYS:
                    ev["_days_away"] = delta
                    upcoming.append(ev)
            except Exception:
                pass
        return sorted(upcoming, key=lambda x: x["_days_away"])

    def _send_alert(self, subject, body_html, gmail_user, gmail_pass):
        msg = MIMEMultipart("alternative")
        msg["From"]    = gmail_user
        msg["To"]      = self.RECIPIENT
        msg["Subject"] = subject
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(gmail_user, gmail_pass)
            srv.sendmail(gmail_user, self.RECIPIENT, msg.as_string())
        print(f"[OK] Alerte envoyée à {self.RECIPIENT}")

    def run(self):
        today_str  = date.today().isoformat()
        sika_hist  = self.load_json_path(self.SIKA_HIST) or {}
        nav_latest = self.load_json_path(self.NAV_LATEST) or {}
        etf_weights = self._get_etf_weights(nav_latest)

        live_prices = self._scrape_live_prices()
        anomalies = []

        all_tickers = set(etf_weights.keys()) | set(sika_hist.keys())
        for tk in sorted(all_tickers):
            prev_close = self._get_prev_close(sika_hist, tk, today_str)
            live       = live_prices.get(tk)
            if not prev_close or not live or prev_close <= 0:
                continue
            chg = (live - prev_close) / prev_close

            severity = None
            if abs(chg) >= self.THRESHOLD_SPLIT:
                severity = "CRITIQUE"
            elif chg <= -self.THRESHOLD_DIV:
                severity = "ATTENTION"

            if severity:
                w_etf = etf_weights.get(tk, 0)
                impact_inav = round(w_etf / 100 * chg * 100, 3)
                anomalies.append({
                    "ticker":      tk,
                    "prev_close":  prev_close,
                    "live":        live,
                    "chg_pct":     round(chg * 100, 2),
                    "w_etf_pct":   round(w_etf, 2),
                    "impact_inav": impact_inav,
                    "severity":    severity,
                })

        anomalies.sort(key=lambda x: abs(x["chg_pct"]), reverse=True)

        calendar_alerts = self._check_calendar(today_str)

        if not anomalies and not calendar_alerts:
            print(f"[{today_str}] Aucune action corporative détectée.")
            return

        secrets    = self.load_json_path(os.path.join(self.scripts_dir, "secrets.json")) or {}
        gmail_user = os.environ.get("GMAIL_USER") or secrets.get("smtp_user")
        gmail_pass = os.environ.get("GMAIL_APP_PASSWORD") or secrets.get("smtp_password")
        if not gmail_user or not gmail_pass:
            print("[WARN] Pas de credentials Gmail — affichage console uniquement.")
            for a in anomalies:
                print(f"  [{a['severity']}] {a['ticker']} : {a['chg_pct']:+.2f}% "
                      f"(prev={a['prev_close']} → live={a['live']}) "
                      f"poids ETF={a['w_etf_pct']}% impact iNAV={a['impact_inav']:+.3f}%")
            for ev in calendar_alerts:
                print(f"  [CALENDRIER J+{ev['_days_away']}] {ev['ticker']} — {ev['type']} "
                      f"ex-date {ev['ex_date']}")
            return

        rows_anomalies = ""
        for a in anomalies:
            color  = "#c0392b" if a["severity"] == "CRITIQUE" else "#e67e22"
            bg     = "#fdf2f2" if a["severity"] == "CRITIQUE" else "#fef9f0"
            rows_anomalies += f"""
        <tr style="background:{bg}">
          <td style="padding:8px 12px;font-weight:700;color:{color}">{a['severity']}</td>
          <td style="padding:8px 12px;font-weight:700">{a['ticker']}</td>
          <td style="padding:8px 12px">{a['prev_close']:,.0f} FCFA</td>
          <td style="padding:8px 12px">{a['live']:,.0f} FCFA</td>
          <td style="padding:8px 12px;font-weight:700;color:{color}">{a['chg_pct']:+.2f}%</td>
          <td style="padding:8px 12px">{a['w_etf_pct']:.2f}%</td>
          <td style="padding:8px 12px;font-weight:700;color:{color}">{a['impact_inav']:+.3f}%</td>
        </tr>"""

        rows_calendar = ""
        for ev in calendar_alerts:
            j = ev["_days_away"]
            label = "AUJOURD'HUI" if j == 0 else f"J+{j}"
            detail = ""
            if ev.get("amount_fcfa"):
                detail = f"Dividende : {ev['amount_fcfa']} FCFA/action"
            elif ev.get("split_ratio"):
                detail = f"Split {ev['split_ratio']}-pour-1"
            elif ev.get("note"):
                detail = ev["note"]
            rows_calendar += f"""
        <tr>
          <td style="padding:8px 12px;font-weight:700;color:#2980b9">{label}</td>
          <td style="padding:8px 12px;font-weight:700">{ev['ticker']}</td>
          <td style="padding:8px 12px">{ev['type'].upper()}</td>
          <td style="padding:8px 12px">{ev['ex_date']}</td>
          <td style="padding:8px 12px">{detail}</td>
        </tr>"""

        section_anomalies = ""
        if anomalies:
            section_anomalies = f"""
        <h3 style="color:#c0392b;margin-top:24px">⚠ Anomalies de prix détectées</h3>
        <p style="color:#666;font-size:13px">
          Prix au scraping de 9h00 UTC vs clôture officielle de la veille.<br>
          Vérifier si un dividende ou un split a été détaché ce matin.
        </p>
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          <thead>
            <tr style="background:#f3f4f6;color:#374151">
              <th style="padding:8px 12px;text-align:left">Niveau</th>
              <th style="padding:8px 12px;text-align:left">Ticker</th>
              <th style="padding:8px 12px;text-align:left">Clôture J-1</th>
              <th style="padding:8px 12px;text-align:left">Prix 9h00</th>
              <th style="padding:8px 12px;text-align:left">Variation</th>
              <th style="padding:8px 12px;text-align:left">Poids ETF</th>
              <th style="padding:8px 12px;text-align:left">Impact iNAV</th>
            </tr>
          </thead>
          <tbody>{rows_anomalies}</tbody>
        </table>"""

        section_calendar = ""
        if calendar_alerts:
            section_calendar = f"""
        <h3 style="color:#2980b9;margin-top:24px">📅 Événements du calendrier</h3>
        <table style="border-collapse:collapse;width:100%;font-size:13px">
          <thead>
            <tr style="background:#f3f4f6;color:#374151">
              <th style="padding:8px 12px;text-align:left">Échéance</th>
              <th style="padding:8px 12px;text-align:left">Ticker</th>
              <th style="padding:8px 12px;text-align:left">Type</th>
              <th style="padding:8px 12px;text-align:left">Ex-date</th>
              <th style="padding:8px 12px;text-align:left">Détail</th>
            </tr>
          </thead>
          <tbody>{rows_calendar}</tbody>
        </table>"""

        body_html = f"""
    <html><body style="font-family:Inter,sans-serif;color:#0c1a2e;max-width:800px;margin:0 auto;padding:24px">
      <h2 style="color:#0c1a2e;border-bottom:2px solid #b8973f;padding-bottom:8px">
        CGF BRVM30 ETF — Alerte Actions Corporatives {today_str}
      </h2>
      {section_anomalies}
      {section_calendar}
      <p style="margin-top:24px;font-size:12px;color:#9ca3af">
        Message automatique généré à 9h00 UTC — CGF Bourse Système
      </p>
    </body></html>"""

        subject = f"[ALERTE] CGF ETF — Actions corporatives {today_str}"
        if anomalies:
            n_crit = sum(1 for a in anomalies if a["severity"] == "CRITIQUE")
            subject = f"[{'CRITIQUE' if n_crit else 'ATTENTION'}] CGF ETF — {len(anomalies)} anomalie(s) de prix {today_str}"

        self._send_alert(subject, body_html, gmail_user, gmail_pass)


if __name__ == "__main__":
    CorporateActionsChecker().run()
