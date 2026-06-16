"""
generate_daily_report.py — Rapport journalier CGF BRVM30 ETF
Généré automatiquement après la clôture du marché BRVM (16h00 UTC).
Usage : python generate_daily_report.py [--date YYYY-MM-DD] [--force]
"""

import os, sys, json, re, warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from io import BytesIO
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDFS_DIR = os.path.join(BASE_DIR, 'pdfs')
sys.path.insert(0, BASE_DIR)

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, HRFlowable, PageBreak,
)

PAGE_W, PAGE_H = A4
MARGIN = 1.8 * cm

# ── Couleurs CGF ───────────────────────────────────────────────────────────
CGF_BLUE   = colors.HexColor("#2563eb")
CGF_DARK   = colors.HexColor("#1e3a5f")
CGF_LIGHT  = colors.HexColor("#dbeafe")
CGF_GREEN  = colors.HexColor("#16a34a")
CGF_RED    = colors.HexColor("#dc2626")
CGF_GRAY   = colors.HexColor("#64748b")
CGF_BORDER = colors.HexColor("#e2e8f0")
CGF_ROW    = colors.HexColor("#f8fafc")
CGF_GLIGHT = colors.HexColor("#dcfce7")
CGF_RLIGHT = colors.HexColor("#fee2e2")
WHITE      = colors.white
BLACK      = colors.HexColor("#1e293b")


def _load(fname):
    path = os.path.join(BASE_DIR, fname)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _scrape_sika_variations():
    """Scrape les variations et cours finaux depuis Sika après clôture."""
    try:
        import requests
        from bs4 import BeautifulSoup
        resp = requests.get(
            'https://sikafinance.com/marches/aaz',
            headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'fr-FR'},
            verify=False, timeout=15,
        )
        soup = BeautifulSoup(resp.text, 'html.parser')
        results = {}
        for a in soup.find_all('a', href=re.compile(r'/marches/cotation_[A-Z]', re.I)):
            m = re.search(r'cotation_([A-Z0-9]+)', a['href'], re.I)
            if not m:
                continue
            ticker = m.group(1).upper()
            if any(x in ticker for x in ('BRVM', 'SIKA', 'COMPO')):
                continue
            row = a.find_parent('tr')
            if not row:
                continue
            cells = row.find_all(['td', 'th'])
            def _p(c):
                return c.get_text(strip=True).replace('\xa0','').replace(' ','').replace(',','.').replace('%','')
            if len(cells) >= 8:
                try:
                    results[ticker] = {
                        'dernier':   float(_p(cells[6])),
                        'variation': float(_p(cells[7])),
                    }
                except (ValueError, IndexError):
                    pass
        return results
    except Exception as e:
        print(f"  Avertissement scraping Sika : {e}")
        return {}


def _chart_intraday(snapshots, par, report_date):
    """Graphique iNAV intraday. Retourne BytesIO PNG."""
    fig, ax = plt.subplots(figsize=(13, 3.5))
    fig.patch.set_facecolor('#ffffff')
    ax.set_facecolor('#f8fafc')

    times  = [s['time'] for s in snapshots]
    vl_pts = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0)
              for s in snapshots]

    xs = range(len(times))
    ax.plot(xs, vl_pts, color='#2563eb', linewidth=2.2, zorder=3)
    ax.fill_between(xs, vl_pts, min(vl_pts) * 0.9995,
                    alpha=0.10, color='#2563eb')
    ax.axhline(y=par, color='#94a3b8', linestyle='--', linewidth=1, alpha=0.7,
               label=f"Émission {par:,.0f}")

    step = max(1, len(times) // 10)
    ax.set_xticks(list(xs)[::step])
    ax.set_xticklabels(times[::step], rotation=45, ha='right', fontsize=7.5)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:,.0f}'))
    ax.set_ylabel('FCFA / part', fontsize=8.5, color='#64748b')
    ax.set_xlabel('Heure UTC', fontsize=8.5, color='#64748b')
    ax.tick_params(colors='#64748b', labelsize=7.5)
    ax.legend(fontsize=7.5, framealpha=0.4)
    for sp in ['top', 'right']:
        ax.spines[sp].set_visible(False)
    for sp in ['left', 'bottom']:
        ax.spines[sp].set_color('#e2e8f0')
    ax.grid(axis='y', color='#e2e8f0', linewidth=0.5)

    plt.tight_layout(pad=0.4)
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf


def _chart_base100(intra_hist, launch_date, nav_at_launch, par):
    """Graphique ETF vs BRVM30 base 100 depuis lancement. Retourne BytesIO PNG."""
    fig, ax = plt.subplots(figsize=(13, 3.5))
    fig.patch.set_facecolor('#ffffff')
    ax.set_facecolor('#f8fafc')

    launch_ts = pd.Timestamp(launch_date)
    etf_pts, idx_pts = {}, {}
    for day, pts in sorted(intra_hist.items()):
        if not pts or pd.Timestamp(day) < launch_ts:
            continue
        lp = pts[-1]
        vl = lp.get('vl_fcfa') or lp.get('vl')
        ni = lp.get('nav_indice')
        if vl:
            etf_pts[pd.Timestamp(day)] = float(vl) / par * 100
        if ni and nav_at_launch:
            idx_pts[pd.Timestamp(day)] = float(ni) / nav_at_launch * 100

    etf_pts[launch_ts] = 100.0
    if nav_at_launch:
        idx_pts[launch_ts] = 100.0

    etf_s = pd.Series(etf_pts).sort_index()
    idx_s = pd.Series(idx_pts).sort_index()
    labels = [d.strftime('%d/%m') for d in etf_s.index]
    xs = range(len(etf_s))

    ax.plot(xs, etf_s.values, color='#2563eb', linewidth=2.2,
            marker='o', markersize=5, label='CGF BRVM30 ETF', zorder=3)
    if not idx_s.empty and len(idx_s) == len(etf_s):
        ax.plot(xs, idx_s.values, color='#64748b', linewidth=1.8,
                linestyle='--', marker='o', markersize=4, label='BRVM30', zorder=2)
    ax.axhline(y=100, color='#94a3b8', linestyle=':', linewidth=1)

    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=7.5)
    ax.set_ylabel('Base 100', fontsize=8.5, color='#64748b')
    ax.tick_params(colors='#64748b', labelsize=7.5)
    ax.legend(fontsize=8, framealpha=0.4)
    for sp in ['top', 'right']:
        ax.spines[sp].set_visible(False)
    for sp in ['left', 'bottom']:
        ax.spines[sp].set_color('#e2e8f0')
    ax.grid(axis='y', color='#e2e8f0', linewidth=0.5)

    plt.tight_layout(pad=0.4)
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close()
    buf.seek(0)
    return buf


def _styles():
    cw = PAGE_W - 2 * MARGIN
    return {
        'title':    ParagraphStyle('rpt_title',   fontName='Helvetica-Bold', fontSize=20, textColor=CGF_DARK, spaceAfter=2, leading=24),
        'subtitle': ParagraphStyle('rpt_sub',     fontName='Helvetica',      fontSize=10, textColor=CGF_GRAY, spaceAfter=2),
        'h2':       ParagraphStyle('rpt_h2',      fontName='Helvetica-Bold', fontSize=12, textColor=CGF_DARK, spaceBefore=12, spaceAfter=5),
        'h3':       ParagraphStyle('rpt_h3',      fontName='Helvetica-Bold', fontSize=10, textColor=CGF_BLUE, spaceBefore=8,  spaceAfter=4),
        'body':     ParagraphStyle('rpt_body',    fontName='Helvetica',      fontSize=9,  textColor=BLACK, leading=13),
        'note':     ParagraphStyle('rpt_note',    fontName='Helvetica-Oblique', fontSize=7.5, textColor=CGF_GRAY, leading=10),
        'rdate':    ParagraphStyle('rpt_date',    fontName='Helvetica',      fontSize=10, textColor=CGF_GRAY, alignment=TA_RIGHT, leading=14),
        'mv_big':   ParagraphStyle('rpt_mv_big',  fontName='Helvetica-Bold', fontSize=15, textColor=CGF_DARK, alignment=TA_CENTER, leading=18),
        'mv_pos':   ParagraphStyle('rpt_mv_pos',  fontName='Helvetica-Bold', fontSize=15, textColor=CGF_GREEN, alignment=TA_CENTER, leading=18),
        'mv_neg':   ParagraphStyle('rpt_mv_neg',  fontName='Helvetica-Bold', fontSize=15, textColor=CGF_RED,   alignment=TA_CENTER, leading=18),
        'mv_neu':   ParagraphStyle('rpt_mv_neu',  fontName='Helvetica-Bold', fontSize=15, textColor=CGF_GRAY,  alignment=TA_CENTER, leading=18),
        'mv_label': ParagraphStyle('rpt_mv_lbl',  fontName='Helvetica',      fontSize=7.5, textColor=CGF_GRAY, alignment=TA_CENTER, leading=9),
    }


def _val_style(S, val):
    if val is None:
        return S['mv_big']
    return S['mv_pos'] if val > 0 else (S['mv_neg'] if val < 0 else S['mv_neu'])


def _fmt_pct(v, decimals=3):
    if v is None:
        return '—'
    sign = '+' if v > 0 else ''
    return f'{sign}{v:.{decimals}f}%'


def generate(report_date: str = None, force: bool = False) -> str:
    """
    Génère le rapport journalier PDF.
    Retourne le chemin du PDF généré.
    """
    os.makedirs(PDFS_DIR, exist_ok=True)

    if report_date is None:
        report_date = datetime.now().strftime('%Y-%m-%d')

    pdf_path = os.path.join(PDFS_DIR, f'rapport_journalier_{report_date}.pdf')
    if os.path.exists(pdf_path) and not force:
        print(f"Rapport déjà existant : {pdf_path}")
        return pdf_path

    # ── Chargement des données ─────────────────────────────────────────────
    print("Chargement des données...")
    nl     = _load('nav_latest.json')           or {}
    intra  = _load('intraday_nav.json')          or {}
    ih     = _load('nav_intraday_history.json')  or {}
    launch = _load('launch_state.json')          or {}

    par           = float(launch.get('par_fcfa', 100000))
    nav_at_launch = float(launch.get('nav_index_at_launch', 0))
    launch_date   = launch.get('launch_date', report_date)
    n_parts       = int(launch.get('n_parts', 0))

    # Snapshots du jour (priorité : nav_intraday_history puis intraday_nav)
    today_snaps = ih.get(report_date) or intra.get('snapshots', [])
    last_snap   = today_snaps[-1] if today_snaps else {}

    vl_cloture  = float(last_snap.get('vl_live_fcfa') or last_snap.get('vl_fcfa') or
                        last_snap.get('vl') or par)
    _c1d        = last_snap.get('change_1d_pct')
    var_jour    = _c1d if _c1d is not None else last_snap.get('change_day_pct')
    aum         = float(last_snap.get('aum_mfcfa') or nl.get('aum_mfcfa') or 0)
    perf_launch = last_snap.get('perf_since_launch')
    nav_indice  = float(last_snap.get('nav_indice') or nl.get('nav_indice') or 0)
    n_prices    = int(last_snap.get('n_prices') or 0)
    heure_cloture = last_snap.get('time', '—')

    # Perf BRVM30 depuis lancement
    perf_idx = (nav_indice / nav_at_launch - 1) * 100 if nav_at_launch and nav_indice else None

    # TE / TD
    launch_ts  = pd.Timestamp(launch_date)
    closes_etf = {}
    closes_idx = {}
    for d, pts in ih.items():
        if pts and pd.Timestamp(d) >= launch_ts:
            lp = pts[-1]
            vl = lp.get('vl_fcfa') or lp.get('vl')
            ni = lp.get('nav_indice')
            if vl: closes_etf[d] = float(vl)
            if ni: closes_idx[d] = float(ni)

    te = td = None
    etf_cl   = pd.Series(closes_etf).sort_index()
    idx_cl   = pd.Series(closes_idx).sort_index()
    n_seances = len(etf_cl)
    if len(etf_cl) >= 2 and len(idx_cl) >= 2:
        ret_etf = etf_cl.pct_change().dropna()
        ret_idx = idx_cl.pct_change().dropna()
        common  = ret_etf.index.intersection(ret_idx.index)
        if len(common) >= 2:
            active = ret_etf.loc[common] - ret_idx.loc[common]
            te = float(active.std() * np.sqrt(252) * 100)
        if nav_at_launch and not etf_cl.empty and not idx_cl.empty:
            etf_cum = etf_cl.iloc[-1] / par
            idx_cum = idx_cl.iloc[-1] / nav_at_launch
            td = (etf_cum / idx_cum - 1) * 100

    # Prochain rebalancement
    last_rebal = nl.get('last_rebal_date')
    next_rebal_str = '—'
    days_rebal = None
    if last_rebal:
        try:
            from dateutil.relativedelta import relativedelta
            lr = pd.Timestamp(last_rebal)
            nr = lr + relativedelta(months=3)
            while nr.weekday() >= 5:
                nr += pd.Timedelta(days=1)
            next_rebal_str = nr.strftime('%d/%m/%Y')
            days_rebal = (nr - pd.Timestamp(report_date).normalize()).days
        except Exception:
            pass

    jours_lct = (pd.Timestamp(report_date) - pd.Timestamp(launch_date)).days

    # Variations Sika
    print("Scraping Sika pour les cours de clôture...")
    sika = _scrape_sika_variations()

    basket = nl.get('basket', [])

    # ── Construction PDF ───────────────────────────────────────────────────
    print("Génération du PDF...")
    S        = _styles()
    cw       = PAGE_W - 2 * MARGIN

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=f'CGF BRVM30 ETF — Rapport journalier {report_date}',
        author='CGF Bourse',
    )

    story = []

    # ══════════════════════════════════════════════════════════════════════
    # PAGE 1 — RÉSUMÉ DE SÉANCE
    # ══════════════════════════════════════════════════════════════════════

    # En-tête
    try:
        date_fr = pd.Timestamp(report_date).strftime('%A %d %B %Y').capitalize()
    except Exception:
        date_fr = report_date

    header = Table(
        [[Paragraph('CGF BRVM30 ETF', S['title']),
          Paragraph(f'Rapport journalier<br/>{date_fr}', S['rdate'])]],
        colWidths=[cw * 0.60, cw * 0.40],
    )
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(HRFlowable(width=cw, thickness=3, color=CGF_BLUE, spaceAfter=8))
    story.append(Paragraph(
        f'Séance du {date_fr} · Jour {jours_lct + 1} depuis le lancement · '
        f'{n_parts:,} parts émises · Prix d\'émission : {par:,.0f} FCFA · '
        f'Dernier iNAV : {heure_cloture} UTC',
        S['subtitle'],
    ))
    story.append(Spacer(1, 8))

    # ── Bloc métriques clés ────────────────────────────────────────────
    story.append(Paragraph('Données de clôture', S['h2']))

    def _mcell(val_str, label_str, style_key):
        return [Paragraph(val_str, S[style_key]),
                Spacer(1, 2),
                Paragraph(label_str, S['mv_label'])]

    def _vl_style(v):
        if v is None: return 'mv_big'
        return 'mv_pos' if v > 0 else ('mv_neg' if v < 0 else 'mv_neu')

    row1_vals = [
        _mcell(f'{vl_cloture:,.0f} FCFA', 'VL de clôture', 'mv_big'),
        _mcell(_fmt_pct(var_jour), 'Variation du jour', _vl_style(var_jour)),
        _mcell(f'{aum:,.1f} MFCFA', 'AUM indicatif', 'mv_big'),
        _mcell(f'{n_prices} / 26', 'Prix utilisés', 'mv_big'),
    ]
    row1_lbls = ['', '', '', '']  # labels intégrés dans _mcell

    m1 = Table([row1_vals], colWidths=[cw / 4] * 4)
    m1.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), CGF_LIGHT),
        ('BOX', (0, 0), (-1, -1), 1, CGF_BORDER),
        ('LINEAFTER', (0, 0), (2, 0), 0.5, CGF_BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(m1)
    story.append(Spacer(1, 6))

    row2_vals = [
        _mcell(_fmt_pct(perf_launch), f'Perf. ETF depuis le {launch_date}', _vl_style(perf_launch)),
        _mcell(_fmt_pct(perf_idx),    'Perf. BRVM30 même période',           _vl_style(perf_idx)),
        _mcell(f'{nav_indice:.4f}' if nav_indice else '—', 'NAV indice BRVM30', 'mv_big'),
        _mcell(f'{n_parts:,}',         'Parts en circulation',                 'mv_big'),
    ]
    m2 = Table([row2_vals], colWidths=[cw / 4] * 4)
    m2.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), WHITE),
        ('BOX', (0, 0), (-1, -1), 1, CGF_BORDER),
        ('LINEAFTER', (0, 0), (2, 0), 0.5, CGF_BORDER),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(m2)
    story.append(Spacer(1, 10))

    # ── TE / TD ────────────────────────────────────────────────────────
    story.append(Paragraph('Métriques de réplication', S['h2']))

    te_str = f'{te:.4f}%' if te is not None else '— (min. 2 séances)'
    td_str = _fmt_pct(td, 4) if td is not None else '— (min. 2 séances)'
    dr_str = f'{days_rebal}j' if days_rebal is not None else '—'

    te_data = [
        ['Tracking Error (TE)',      te_str,        'Séances enregistrées', f'{n_seances}'],
        ['Tracking Difference (TD)', td_str,        'Prochain rebalancement', next_rebal_str],
        ['Jours depuis lancement',   f'{jours_lct}j', 'Jours avant rebalancement', dr_str],
    ]
    te_tbl = Table(te_data, colWidths=[cw * 0.28, cw * 0.22, cw * 0.28, cw * 0.22])
    te_tbl.setStyle(TableStyle([
        ('FONTNAME',   (0, 0), (-1, -1), 'Helvetica'),
        ('FONTNAME',   (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME',   (2, 0), (2, -1), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 9),
        ('TEXTCOLOR',  (0, 0), (-1, -1), BLACK),
        ('BACKGROUND', (0, 0), (0, -1), CGF_LIGHT),
        ('BACKGROUND', (2, 0), (2, -1), CGF_LIGHT),
        ('BOX',        (0, 0), (-1, -1), 1, CGF_BORDER),
        ('INNERGRID',  (0, 0), (-1, -1), 0.5, CGF_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(te_tbl)

    if te is None:
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            'TE et TD affichés dès la 2ème séance complète. '
            'TE = écart-type annualisé des rendements actifs (×√252). '
            'TD = (VL ETF cumulée / BRVM30 cumulé) − 1.',
            S['note'],
        ))

    # ══════════════════════════════════════════════════════════════════════
    # PAGE 2 — GRAPHIQUES
    # ══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph('Graphiques de séance', S['h2']))
    story.append(HRFlowable(width=cw, thickness=1, color=CGF_BORDER, spaceAfter=8))

    # Graphique iNAV intraday
    if today_snaps:
        story.append(Paragraph(
            f'iNAV intraday — séance du {date_fr} ({len(today_snaps)} points)', S['h3'],
        ))
        buf1 = _chart_intraday(today_snaps, par, report_date)
        story.append(Image(buf1, width=cw, height=cw * 3.5 / 13))
        vl_vals = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0)
                   for s in today_snaps]
        story.append(Paragraph(
            f'Min : {min(vl_vals):,.0f} FCFA · Max : {max(vl_vals):,.0f} FCFA · '
            f'Clôture : {vl_cloture:,.0f} FCFA · Ligne pointillée = prix d\'émission',
            S['note'],
        ))
    else:
        story.append(Paragraph('Aucune donnée iNAV intraday disponible pour cette séance.', S['note']))

    story.append(Spacer(1, 14))

    # Graphique ETF vs BRVM30 base 100
    story.append(Paragraph('ETF vs BRVM30 — performance relative depuis le lancement (base 100)', S['h3']))
    n_days_data = sum(1 for d, pts in ih.items()
                      if pts and pd.Timestamp(d) >= launch_ts)
    if n_days_data >= 1:
        buf2 = _chart_base100(ih, launch_date, nav_at_launch, par)
        story.append(Image(buf2, width=cw, height=cw * 3.5 / 13))
        story.append(Paragraph(
            f'Base 100 = prix d\'émission ({par:,.0f} FCFA) au {launch_date}. '
            f'{n_days_data} séance(s) de données disponibles.',
            S['note'],
        ))
    else:
        story.append(Paragraph('Données insuffisantes pour le graphique base 100.', S['note']))

    # ══════════════════════════════════════════════════════════════════════
    # PAGE 3 — COMPOSITION DU PORTEFEUILLE
    # ══════════════════════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph('Composition du portefeuille', S['h2']))
    story.append(HRFlowable(width=cw, thickness=1, color=CGF_BORDER, spaceAfter=6))
    story.append(Paragraph(
        f'Panier BRVM30 · {len(basket)} titres · '
        f'Données de clôture : {nl.get("calc_date", "—")} · '
        f'Cours live : sikafinance.com',
        S['note'],
    ))
    story.append(Spacer(1, 6))

    # Tableau portefeuille
    col_w = [cw * x for x in [0.12, 0.09, 0.16, 0.12, 0.16, 0.35]]
    headers = ['Ticker', 'Poids', 'Clôture J-1', 'Var. J (%)', 'Cours live', 'Valeur (MFCFA)']

    tbl_data   = [headers]
    variations = []

    for r in basket:
        ticker = r['ticker'].upper()
        sk     = sika.get(ticker, {})
        var_j  = sk.get('variation') if isinstance(sk, dict) else None
        cours  = sk.get('dernier')   if isinstance(sk, dict) else None
        variations.append(var_j)

        tbl_data.append([
            ticker,
            f"{r['poids_pct']:.2f}%",
            f"{int(r['dernier_prix']):,}" if r.get('dernier_prix') else '—',
            f'{var_j:+.2f}%' if var_j is not None else '—',
            f"{int(cours):,}" if cours else '—',
            f"{r['pv_mfcfa']:.1f}",
        ])

    port_tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)

    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), CGF_DARK),
        ('TEXTCOLOR',  (0, 0), (-1, 0), WHITE),
        ('FONTNAME',   (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE',   (0, 0), (-1, -1), 8),
        ('ALIGN',      (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN',     (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('BOX',        (0, 0), (-1, -1), 1, CGF_BORDER),
        ('LINEBELOW',  (0, 0), (-1, 0),  1, CGF_BLUE),
        ('GRID',       (0, 1), (-1, -1), 0.5, CGF_BORDER),
    ]

    for i in range(1, len(tbl_data)):
        if i % 2 == 0:
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), CGF_ROW))
        v = variations[i - 1]
        if v is not None and v > 0:
            style_cmds.append(('TEXTCOLOR', (3, i), (3, i), CGF_GREEN))
            style_cmds.append(('FONTNAME',  (3, i), (3, i), 'Helvetica-Bold'))
        elif v is not None and v < 0:
            style_cmds.append(('TEXTCOLOR', (3, i), (3, i), CGF_RED))
            style_cmds.append(('FONTNAME',  (3, i), (3, i), 'Helvetica-Bold'))

    port_tbl.setStyle(TableStyle(style_cmds))
    story.append(port_tbl)
    story.append(Spacer(1, 12))

    # Top 5 hausses / baisses
    basket_var = [
        (r['ticker'].upper(), r['poids_pct'],
         sika.get(r['ticker'].upper(), {}).get('variation'))
        for r in basket
        if isinstance(sika.get(r['ticker'].upper(), {}), dict)
        and sika.get(r['ticker'].upper(), {}).get('variation') is not None
    ]

    if basket_var:
        story.append(Paragraph('Top mouvements du jour', S['h3']))
        top5    = sorted(basket_var, key=lambda x: x[2], reverse=True)[:5]
        bottom5 = sorted(basket_var, key=lambda x: x[2])[:5]

        tb_data = [
            ['Top 5 hausses', '', '', 'Top 5 baisses', '', ''],
            ['Ticker', 'Poids', 'Variation', 'Ticker', 'Poids', 'Variation'],
        ]
        for i in range(5):
            row = []
            if i < len(top5):
                t, p, v = top5[i]
                row += [t, f'{p:.2f}%', f'+{v:.2f}%']
            else:
                row += ['—', '—', '—']
            if i < len(bottom5):
                t, p, v = bottom5[i]
                row += [t, f'{p:.2f}%', f'{v:.2f}%']
            else:
                row += ['—', '—', '—']
            tb_data.append(row)

        cw2 = cw / 6
        tb_tbl = Table(tb_data, colWidths=[cw2] * 6, repeatRows=2)
        tb_style = [
            ('SPAN', (0, 0), (2, 0)), ('SPAN', (3, 0), (5, 0)),
            ('BACKGROUND', (0, 0), (2, 0), CGF_GREEN),
            ('BACKGROUND', (3, 0), (5, 0), CGF_RED),
            ('TEXTCOLOR',  (0, 0), (5, 0), WHITE),
            ('BACKGROUND', (0, 1), (2, 1), CGF_GLIGHT),
            ('BACKGROUND', (3, 1), (5, 1), CGF_RLIGHT),
            ('FONTNAME',   (0, 0), (-1, 1), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, 0), (-1, -1), 8),
            ('ALIGN',      (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',     (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('BOX',   (0, 0), (-1, -1), 1, CGF_BORDER),
            ('GRID',  (0, 0), (-1, -1), 0.5, CGF_BORDER),
        ]
        for i in range(2, len(tb_data)):
            if i - 2 < len(top5):
                tb_style.append(('TEXTCOLOR', (2, i), (2, i), CGF_GREEN))
                tb_style.append(('FONTNAME',  (2, i), (2, i), 'Helvetica-Bold'))
            if i - 2 < len(bottom5):
                tb_style.append(('TEXTCOLOR', (5, i), (5, i), CGF_RED))
                tb_style.append(('FONTNAME',  (5, i), (5, i), 'Helvetica-Bold'))
        tb_tbl.setStyle(TableStyle(tb_style))
        story.append(tb_tbl)

    # ── Pied de page ───────────────────────────────────────────────────
    story.append(Spacer(1, 16))
    story.append(HRFlowable(width=cw, thickness=0.5, color=CGF_BORDER, spaceAfter=5))
    story.append(Paragraph(
        f'CGF BRVM30 ETF · Rapport généré le {datetime.now().strftime("%d/%m/%Y à %H:%M")} UTC · '
        f'Source : sikafinance.com · Pour usage interne uniquement. '
        f'Les performances passées ne préjugent pas des performances futures.',
        S['note'],
    ))

    doc.build(story)
    print(f'Rapport généré : {pdf_path}')
    return pdf_path


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Rapport journalier CGF BRVM30 ETF')
    parser.add_argument('--date',  default=None,        help='Date YYYY-MM-DD (défaut : aujourd\'hui)')
    parser.add_argument('--force', action='store_true', help='Regénérer même si déjà existant')
    args = parser.parse_args()
    generate(report_date=args.date, force=args.force)
