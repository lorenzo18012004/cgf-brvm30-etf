"""
send_report_email.py — Envoi du rapport journalier PDF
=======================================================
- En local  : utilise Outlook COM (pas de mot de passe nécessaire)
- Cloud     : utilise Gmail SMTP via variables d'env GMAIL_USER + GMAIL_APP_PASSWORD

Usage : python send_report_email.py [--date YYYY-MM-DD]
"""
import os, sys, argparse, datetime, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base      import MIMEBase
from email.mime.text      import MIMEText
from email                import encoders

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
RECIPIENT = "l.philippe@cgfgestion.com"


def _send_gmail(pdf_path: str, date_str: str, gmail_user: str, gmail_pass: str) -> bool:
    msg = MIMEMultipart()
    msg['From']    = gmail_user
    msg['To']      = RECIPIENT
    msg['Subject'] = f"CGF BRVM30 ETF — Rapport journalier {date_str}"

    body = (
        f"Bonjour,\n\n"
        f"Veuillez trouver ci-joint le rapport journalier du fonds CGF BRVM30 ETF "
        f"pour la séance du {date_str}.\n\n"
        f"Cordialement,\n"
        f"CGF Bourse — Système automatique"
    )
    msg.attach(MIMEText(body, 'plain'))

    with open(pdf_path, 'rb') as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(pdf_path)}"')
    msg.attach(part)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, RECIPIENT, msg.as_string())

    print(f"[OK] Email envoyé via Gmail à {RECIPIENT}")
    return True


def _send_outlook(pdf_path: str, date_str: str) -> bool:
    import win32com.client
    outlook = win32com.client.Dispatch("Outlook.Application")
    mail    = outlook.CreateItem(0)
    mail.To      = RECIPIENT
    mail.Subject = f"CGF BRVM30 ETF — Rapport journalier {date_str}"
    mail.Body    = (
        f"Bonjour,\n\n"
        f"Veuillez trouver ci-joint le rapport journalier du fonds CGF BRVM30 ETF "
        f"pour la séance du {date_str}.\n\n"
        f"Cordialement,\n"
        f"CGF Bourse — Système automatique"
    )
    mail.Attachments.Add(pdf_path)
    mail.Send()
    print(f"[OK] Email envoyé via Outlook à {RECIPIENT}")
    return True


def _load_secrets() -> dict:
    """Charge secrets.json (non commité) pour les credentials Gmail."""
    import json
    path = os.path.join(BASE_DIR, "secrets.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return {}


def send(date_str: str | None = None) -> bool:
    if date_str is None:
        date_str = datetime.date.today().strftime("%Y-%m-%d")

    pdf_path = os.path.join(BASE_DIR, "pdfs", f"rapport_journalier_{date_str}.pdf")
    if not os.path.exists(pdf_path):
        print(f"[ERREUR] PDF introuvable : {pdf_path}")
        return False

    try:
        secrets    = _load_secrets()
        gmail_user = os.environ.get('GMAIL_USER') or secrets.get('smtp_user')
        gmail_pass = os.environ.get('GMAIL_APP_PASSWORD') or secrets.get('smtp_password')
        if gmail_user and gmail_pass and gmail_pass != "REMPLACER_PAR_APP_PASSWORD":
            return _send_gmail(pdf_path, date_str, gmail_user, gmail_pass)
        else:
            return _send_outlook(pdf_path, date_str)
    except Exception as e:
        print(f"[ERREUR] Envoi email échoué : {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    args = parser.parse_args()
    ok = send(args.date)
    sys.exit(0 if ok else 1)
