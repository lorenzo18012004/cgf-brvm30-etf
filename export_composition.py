"""
export_composition.py — Export composition du panier CGF BRVM30 ETF
====================================================================
Génère deux fichiers :
  - composition_YYYYMMDD.csv   (pour publication réglementaire)
  - composition_YYYYMMDD.pdf   (document formel)

Usage :
    python export_composition.py
    python export_composition.py --output-dir ./exports
"""

import sys, io, os, json, argparse, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
warnings.filterwarnings('ignore')

import pandas as pd
from datetime import datetime

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
NAV_LATEST  = os.path.join(BASE_DIR, 'nav_latest.json')
LAUNCH_FILE = os.path.join(BASE_DIR, 'launch_state.json')

# ── Noms complets des tickers BRVM ────────────────────────────────────────────
TICKER_NAMES = {
    'SNTS': 'Sonatel',
    'ORAC': 'Orange CI',
    'SGBC': 'Société Générale Bénin/CI',
    'ECOC': 'Ecobank CI',
    'BICB': 'Banque Internationale pour le Commerce du Bénin',
    'STBC': 'Société Générale de Banques au Burkina',
    'SIBC': 'Société Ivoirienne de Banque',
    'BOAB': 'Bank of Africa Bénin',
    'SPHC': 'Saph CI',
    'SOGC': 'Société Générale CI',
    'BICC': 'BICICI',
    'TTLC': 'TotalEnergies Marketing CI',
    'ORGT': 'Orange Guinée',
    'ETIT': 'Etitre',
    'BNBC': 'Bernabé CI',
    'BOABF': 'Bank of Africa Burkina',
    'NSBC': 'Nsia Banque CI',
    'ONTBF': 'ONATEL Burkina',
    'PALC': 'Palmafrique CI',
    'PRSC': 'Prestige CI',
    'SDSC': 'Africa Global Logistics CI',
    'CFAC': 'Air Liquide CI',
    'SIAC': 'SIFCA CI',
    'SMBC': 'SMB CI',
    'SVOC': 'SIVOA CI',
    'NEIC': 'NEI-CEDA CI',
    'FTSC': 'Filtisac CI',
    'ABJC': 'Servair Abidjan CI',
    'BOAM': 'Bank of Africa Mali',
    'BOAN': 'Bank of Africa Niger',
    'BOAS': 'Bank of Africa Sénégal',
    'BOAC': 'Bank of Africa CI',
}

SECTOR_NAMES = {
    'Télécommunications': 'Télécommunications',
    'Services Financiers': 'Services Financiers',
    'Energie': 'Énergie',
    'Distribution': 'Distribution',
    'Agriculture': 'Agriculture',
    'Industrie': 'Industrie',
    'Transport': 'Transport',
}


def _load_data() -> tuple[dict, dict | None]:
    if not os.path.exists(NAV_LATEST):
        raise FileNotFoundError("nav_latest.json introuvable. Lancez : python calc_nav.py")
    with open(NAV_LATEST, encoding='utf-8') as f:
        nl = json.load(f)
    launch = None
    if os.path.exists(LAUNCH_FILE):
        with open(LAUNCH_FILE, encoding='utf-8') as f:
            launch = json.load(f)
    return nl, launch


def _build_df(nl: dict, launch: dict | None) -> pd.DataFrame:
    basket = nl.get('basket', [])
    if not basket:
        raise ValueError("Panier vide dans nav_latest.json")

    aum_mfcfa   = nl.get('aum_mfcfa', 0)
    rebal_date  = nl.get('last_rebal_date', '—')
    prix_date   = nl.get('calc_date', '—')

    rows = []
    for i, item in enumerate(basket, 1):
        ticker = item['ticker']
        poids  = item['poids_pct']
        pv     = item['pv_mfcfa']
        prix   = item.get('dernier_prix')
        rows.append({
            'N°':                  i,
            'Ticker':              ticker,
            'Dénomination':        TICKER_NAMES.get(ticker, ticker),
            'Poids (%)':           round(poids, 2),
            'Val. marché (MFCFA)': round(pv, 1),
            'Dernier cours (FCFA)':int(prix) if prix else '—',
            'Stale':               '⚠' if item.get('prix_stale') else '',
        })

    df = pd.DataFrame(rows)
    return df, aum_mfcfa, rebal_date, prix_date


def export_csv(df: pd.DataFrame, output_path: str, meta: dict) -> None:
    lines = [
        f"# CGF BRVM30 ETF — Composition du panier",
        f"# Date de publication : {meta['pub_date']}",
        f"# Date du dernier rebalancement : {meta['rebal_date']}",
        f"# Cours au : {meta['prix_date']}",
        f"# AUM indicatif : {meta['aum_mfcfa']:,.1f} MFCFA",
        f"# Nombre de titres : {meta['n_titres']}",
        f"#",
    ]
    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write('\n'.join(lines) + '\n')
        df.to_csv(f, index=False)
    print(f"  CSV : {output_path}")


def export_pdf(df: pd.DataFrame, output_path: str, meta: dict) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )
    styles = getSampleStyleSheet()

    CGF_BLUE  = colors.HexColor('#1e40af')
    CGF_LIGHT = colors.HexColor('#dbeafe')
    GREY_ROW  = colors.HexColor('#f8fafc')
    RED_WARN  = colors.HexColor('#dc2626')

    def style(name, **kw):
        s = ParagraphStyle(name, parent=styles['Normal'], **kw)
        return s

    story = []

    # ── En-tête ──────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "CGF BOURSE — Gestion d'Actifs",
        style('header_sub', fontSize=9, textColor=colors.grey, alignment=TA_CENTER)
    ))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        "CGF BRVM30 ETF",
        style('title', fontSize=18, textColor=CGF_BLUE, alignment=TA_CENTER, fontName='Helvetica-Bold')
    ))
    story.append(Paragraph(
        "COMPOSITION DU PANIER — PUBLICATION RÉGLEMENTAIRE",
        style('subtitle', fontSize=11, textColor=CGF_BLUE, alignment=TA_CENTER, fontName='Helvetica-Bold')
    ))
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width='100%', thickness=2, color=CGF_BLUE))
    story.append(Spacer(1, 0.4*cm))

    # ── Méta-données ─────────────────────────────────────────────────────────
    meta_data = [
        ['Date de publication',        meta['pub_date']],
        ['Dernier rebalancement',       meta['rebal_date']],
        ['Cours au',                    meta['prix_date']],
        ['AUM indicatif',               f"{meta['aum_mfcfa']:,.1f} MFCFA"],
        ['VL par part',                 f"{meta['vl_par_part']:,.0f} FCFA"],
        ['Nombre de titres en panier',  str(meta['n_titres'])],
    ]
    meta_table = Table(meta_data, colWidths=[5.5*cm, 6*cm])
    meta_table.setStyle(TableStyle([
        ('FONTSIZE',    (0,0), (-1,-1), 9),
        ('TEXTCOLOR',   (0,0), (0,-1), colors.grey),
        ('FONTNAME',    (1,0), (1,-1), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.white, GREY_ROW]),
        ('GRID',        (0,0), (-1,-1), 0.3, colors.HexColor('#e2e8f0')),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING',  (0,0), (-1,-1), 4),
        ('BOTTOMPADDING',(0,0),(-1,-1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.6*cm))

    # ── Tableau composition ──────────────────────────────────────────────────
    story.append(Paragraph(
        "Composition détaillée",
        style('sec', fontSize=11, textColor=CGF_BLUE, fontName='Helvetica-Bold')
    ))
    story.append(Spacer(1, 0.2*cm))

    col_headers = ['N°', 'Ticker', 'Dénomination', 'Poids (%)', 'Val. marché\n(MFCFA)', 'Cours\n(FCFA)']
    col_widths   = [0.8*cm, 1.5*cm, 7.5*cm, 1.8*cm, 2.2*cm, 2.2*cm]

    table_data = [col_headers]
    for _, row in df.iterrows():
        warn = ' ⚠' if row.get('Stale') == '⚠' else ''
        denom = row['Dénomination'] + warn
        table_data.append([
            str(row['N°']),
            row['Ticker'],
            denom,
            f"{row['Poids (%)']:.2f}%",
            f"{row['Val. marché (MFCFA)']:,.1f}",
            str(row['Dernier cours (FCFA)']),
        ])

    # Ligne total
    total_poids = df['Poids (%)'].sum()
    total_val   = df['Val. marché (MFCFA)'].sum()
    table_data.append(['', 'TOTAL', '', f"{total_poids:.2f}%", f"{total_val:,.1f}", ''])

    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        # Header
        ('BACKGROUND',   (0,0), (-1,0), CGF_BLUE),
        ('TEXTCOLOR',    (0,0), (-1,0), colors.white),
        ('FONTNAME',     (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',     (0,0), (-1,0), 9),
        ('ALIGN',        (0,0), (-1,0), 'CENTER'),
        # Données
        ('FONTSIZE',     (0,1), (-1,-1), 8.5),
        ('ROWBACKGROUNDS',(0,1),(-1,-2), [colors.white, GREY_ROW]),
        ('ALIGN',        (3,1), (-1,-1), 'RIGHT'),
        ('ALIGN',        (0,1), (1,-1),  'CENTER'),
        # Total
        ('BACKGROUND',   (0,-1), (-1,-1), CGF_LIGHT),
        ('FONTNAME',     (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE',     (0,-1), (-1,-1), 9),
        # Grille
        ('GRID',         (0,0), (-1,-1), 0.3, colors.HexColor('#cbd5e1')),
        ('LINEBELOW',    (0,0), (-1,0), 1, CGF_BLUE),
        ('LINEABOVE',    (0,-1),(-1,-1), 1, colors.HexColor('#94a3b8')),
        # Padding
        ('TOPPADDING',   (0,0), (-1,-1), 4),
        ('BOTTOMPADDING',(0,0), (-1,-1), 4),
        ('LEFTPADDING',  (0,0), (-1,-1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.5*cm))

    # ── Note de bas de page ──────────────────────────────────────────────────
    story.append(HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#cbd5e1')))
    story.append(Spacer(1, 0.2*cm))
    note_style = style('note', fontSize=7.5, textColor=colors.grey)
    story.append(Paragraph(
        "⚠ Les titres marqués ⚠ ont un cours non mis à jour depuis plus de 5 jours ouvrés.",
        note_style
    ))
    story.append(Paragraph(
        "Ce document est publié à titre informatif conformément aux obligations réglementaires "
        "de publication de la composition des ETF cotés sur la BRVM. "
        "Les données de cours sont indicatives et issues de sikafinance.com.",
        note_style
    ))
    story.append(Paragraph(
        f"Document généré le {meta['pub_date']} par CGF BOURSE — Gestion d'Actifs.",
        note_style
    ))

    doc.build(story)
    print(f"  PDF : {output_path}")


def run(output_dir: str | None = None) -> None:
    os.chdir(BASE_DIR)
    out_dir = output_dir or os.path.join(BASE_DIR, 'exports')
    os.makedirs(out_dir, exist_ok=True)

    nl, launch = _load_data()
    df, aum_mfcfa, rebal_date, prix_date = _build_df(nl, launch)

    pub_date = datetime.now().strftime('%d/%m/%Y')
    date_slug = datetime.now().strftime('%Y%m%d')

    meta = {
        'pub_date':    pub_date,
        'rebal_date':  rebal_date,
        'prix_date':   prix_date,
        'aum_mfcfa':   aum_mfcfa,
        'vl_par_part': nl.get('vl_par_part_fcfa', 0),
        'n_titres':    len(df),
    }

    print(f"Export composition — {pub_date}")
    print(f"  {len(df)} titres | AUM {aum_mfcfa:,.0f} MFCFA | Rebal. {rebal_date}")
    print()

    csv_path = os.path.join(out_dir, f'composition_{date_slug}.csv')
    pdf_path = os.path.join(out_dir, f'composition_{date_slug}.pdf')

    export_csv(df, csv_path, meta)
    export_pdf(df, pdf_path, meta)

    print()
    print(f"Dossier : {out_dir}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Export composition CGF BRVM30 ETF')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Dossier de sortie (défaut: ./exports/)')
    args = parser.parse_args()
    run(output_dir=args.output_dir)
