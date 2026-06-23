"""
generate_daily_report.py — Bulletin quotidien de VL CGF BRVM30 ETF
Généré automatiquement après la clôture du marché BRVM (16h00 UTC).
Usage : python generate_daily_report.py [--date YYYY-MM-DD] [--force]
"""

import os, sys, re, warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, HRFlowable, PageBreak, KeepTogether,
)

from base import BaseScript

JOURS_FR = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche']
MOIS_FR  = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin',
            'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']

def _date_fr(date_str):
    t = pd.Timestamp(date_str)
    return f"{JOURS_FR[t.weekday()]} {t.day} {MOIS_FR[t.month - 1]} {t.year}"

def _date_fr_short(date_str):
    t = pd.Timestamp(date_str)
    return f"{t.day:02d}/{t.month:02d}/{t.year}"


class ReportGenerator(BaseScript):
    # Palette sobre, fond blanc
    NAVY   = colors.HexColor("#1e3a5f")
    DARK   = colors.HexColor("#0c1a2e")
    GOLD   = colors.HexColor("#b89b3f")
    GREEN  = colors.HexColor("#15803d")
    RED    = colors.HexColor("#b91c1c")
    GRAY   = colors.HexColor("#64748b")
    LGRAY  = colors.HexColor("#f8f9fa")
    LGOLD  = colors.HexColor("#fdfaf0")
    BORDER = colors.HexColor("#e2e8f0")
    NBORD  = colors.HexColor("#cbd5e1")
    WHITE  = colors.white
    BLACK  = colors.HexColor("#1e293b")

    def __init__(self):
        super().__init__()
        self.PAGE_W, self.PAGE_H = A4
        self.MARGIN   = 1.5 * cm
        self.PDFS_DIR = os.path.join(self.data_dir, 'pdfs')
        self.LOGO     = os.path.join(self.root_dir, '1780762763961.jpg')

    def _load(self, fname):
        path = os.path.join(self.data_dir, fname)
        if not os.path.exists(path):
            return None
        import json
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def _scrape_sika_variations(self):
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

    def _chart_intraday(self, snapshots, par):
        fig, ax = plt.subplots(figsize=(13, 3.2))
        fig.patch.set_facecolor('#ffffff')
        ax.set_facecolor('#fafbfc')

        times  = [s['time'] for s in snapshots]
        vl_pts = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0)
                  for s in snapshots]

        xs = list(range(len(times)))
        ax.plot(xs, vl_pts, color='#1e3a5f', linewidth=1.8, zorder=3)
        ax.fill_between(xs, vl_pts, min(vl_pts) * 0.9995,
                        alpha=0.07, color='#1e3a5f')
        ax.axhline(y=par, color='#b89b3f', linestyle='--', linewidth=1.2,
                   alpha=0.85, label=f"Prix d'émission {par:,.0f} FCFA")

        vl_min, vl_max = min(vl_pts), max(vl_pts)
        i_min = vl_pts.index(vl_min)
        i_max = vl_pts.index(vl_max)
        ax.annotate(f'{vl_min:,.0f}', xy=(i_min, vl_min),
                    xytext=(0, -14), textcoords='offset points',
                    fontsize=6.5, color='#b91c1c', ha='center')
        ax.annotate(f'{vl_max:,.0f}', xy=(i_max, vl_max),
                    xytext=(0, 5), textcoords='offset points',
                    fontsize=6.5, color='#15803d', ha='center')

        step = max(1, len(times) // 8)
        ax.set_xticks(xs[::step])
        ax.set_xticklabels(times[::step], rotation=40, ha='right', fontsize=7)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:,.0f}'))
        ax.tick_params(colors='#64748b', labelsize=7)
        ax.legend(fontsize=7.5, framealpha=0.5, loc='upper left')
        for sp in ['top', 'right']:
            ax.spines[sp].set_visible(False)
        for sp in ['left', 'bottom']:
            ax.spines[sp].set_color('#e2e8f0')
        ax.grid(axis='y', color='#f1f5f9', linewidth=0.6)
        plt.tight_layout(pad=0.3)
        buf = BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf

    def _chart_base100(self, intra_hist, launch_date, brvm30_hist, brvm30_at_launch, par):
        fig, ax = plt.subplots(figsize=(13, 2.6))
        fig.patch.set_facecolor('#ffffff')
        ax.set_facecolor('#fafbfc')

        launch_ts = pd.Timestamp(launch_date)
        etf_pts, idx_pts = {}, {}
        for day, pts in sorted(intra_hist.items()):
            if not pts or pd.Timestamp(day) < launch_ts:
                continue
            lp = pts[-1]
            vl = lp.get('vl_fcfa') or lp.get('vl')
            bv = lp.get('brvm30_official') or brvm30_hist.get(day)
            if vl:
                etf_pts[pd.Timestamp(day)] = float(vl) / par * 100
            if bv and brvm30_at_launch:
                idx_pts[pd.Timestamp(day)] = float(bv) / brvm30_at_launch * 100

        etf_pts[launch_ts] = 100.0
        if brvm30_at_launch:
            idx_pts[launch_ts] = 100.0

        etf_s = pd.Series(etf_pts).sort_index()
        idx_s = pd.Series(idx_pts).sort_index()
        labels = [d.strftime('%d/%m') for d in etf_s.index]
        xs = list(range(len(etf_s)))

        ax.plot(xs, etf_s.values, color='#1e3a5f', linewidth=2.2,
                marker='o', markersize=5, label='CGF BRVM30 ETF', zorder=3)
        if not idx_s.empty:
            idx_al = idx_s.reindex(etf_s.index)
            ax.plot(xs, idx_al.values, color='#b89b3f', linewidth=1.8,
                    linestyle='--', marker='s', markersize=4,
                    label='BRVM30 Indice', zorder=2)
        ax.axhline(y=100, color='#cbd5e1', linestyle=':', linewidth=0.8)

        ax.set_xticks(xs)
        ax.set_xticklabels(labels, fontsize=8.5, ha='center')
        ax.set_ylabel('Base 100', fontsize=7.5, color='#64748b')
        ax.tick_params(colors='#64748b', labelsize=7.5)
        ax.legend(fontsize=8.5, framealpha=0.5, loc='upper left')
        for sp in ['top', 'right']:
            ax.spines[sp].set_visible(False)
        for sp in ['left', 'bottom']:
            ax.spines[sp].set_color('#e2e8f0')
        ax.grid(axis='y', color='#f1f5f9', linewidth=0.6)
        plt.tight_layout(pad=0.3)
        buf = BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        plt.close()
        buf.seek(0)
        return buf

    def _styles(self):
        return {
            # Titres
            'fund_name':  ParagraphStyle('fn', fontName='Helvetica-Bold', fontSize=14,
                                         textColor=self.NAVY, leading=17, alignment=TA_RIGHT),
            'doc_type':   ParagraphStyle('dt', fontName='Helvetica-Bold', fontSize=8,
                                         textColor=self.GOLD, leading=10,
                                         alignment=TA_RIGHT),
            'date_main':  ParagraphStyle('dm', fontName='Helvetica', fontSize=9,
                                         textColor=self.GRAY, leading=12, alignment=TA_RIGHT),
            'section_hd': ParagraphStyle('sh', fontName='Helvetica-Bold', fontSize=7.5,
                                         textColor=self.NAVY, leading=10,
                                         spaceAfter=0, spaceBefore=0),
            # KPI
            'kpi_big':    ParagraphStyle('kb', fontName='Helvetica-Bold', fontSize=20,
                                         textColor=self.NAVY, alignment=TA_CENTER, leading=24),
            'kpi_pos':    ParagraphStyle('kp', fontName='Helvetica-Bold', fontSize=20,
                                         textColor=self.GREEN, alignment=TA_CENTER, leading=24),
            'kpi_neg':    ParagraphStyle('kn', fontName='Helvetica-Bold', fontSize=20,
                                         textColor=self.RED, alignment=TA_CENTER, leading=24),
            'kpi_med':    ParagraphStyle('km', fontName='Helvetica-Bold', fontSize=14,
                                         textColor=self.NAVY, alignment=TA_CENTER, leading=17),
            'kpi_mpos':   ParagraphStyle('kmp', fontName='Helvetica-Bold', fontSize=14,
                                         textColor=self.GREEN, alignment=TA_CENTER, leading=17),
            'kpi_mneg':   ParagraphStyle('kmn', fontName='Helvetica-Bold', fontSize=14,
                                         textColor=self.RED, alignment=TA_CENTER, leading=17),
            'kpi_sm':     ParagraphStyle('ks', fontName='Helvetica-Bold', fontSize=11,
                                         textColor=self.NAVY, alignment=TA_CENTER, leading=14),
            'kpi_spos':   ParagraphStyle('ksp', fontName='Helvetica-Bold', fontSize=11,
                                         textColor=self.GREEN, alignment=TA_CENTER, leading=14),
            'kpi_sneg':   ParagraphStyle('ksn', fontName='Helvetica-Bold', fontSize=11,
                                         textColor=self.RED, alignment=TA_CENTER, leading=14),
            'kpi_lbl':    ParagraphStyle('kl', fontName='Helvetica', fontSize=6.5,
                                         textColor=self.GRAY, alignment=TA_CENTER,
                                         leading=8, spaceAfter=0),
            'kpi_lbl_l':  ParagraphStyle('kll', fontName='Helvetica', fontSize=7,
                                         textColor=self.GRAY, alignment=TA_LEFT,
                                         leading=9, spaceAfter=0),
            # Corps
            'body':       ParagraphStyle('body', fontName='Helvetica', fontSize=8.5,
                                         textColor=self.BLACK, leading=12),
            'body_bold':  ParagraphStyle('bb', fontName='Helvetica-Bold', fontSize=8.5,
                                         textColor=self.BLACK, leading=12),
            'note':       ParagraphStyle('note', fontName='Helvetica-Oblique', fontSize=6.5,
                                         textColor=self.GRAY, leading=8.5),
            'note_c':     ParagraphStyle('notec', fontName='Helvetica-Oblique', fontSize=6.5,
                                         textColor=self.GRAY, leading=8.5, alignment=TA_CENTER),
            'tbl_hd':     ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=7.5,
                                         textColor=self.WHITE, alignment=TA_CENTER, leading=9),
            'tbl_val':    ParagraphStyle('tv', fontName='Helvetica', fontSize=7.5,
                                         textColor=self.BLACK, alignment=TA_CENTER, leading=9),
            'tbl_pos':    ParagraphStyle('tvp', fontName='Helvetica-Bold', fontSize=7.5,
                                         textColor=self.GREEN, alignment=TA_CENTER, leading=9),
            'tbl_neg':    ParagraphStyle('tvn', fontName='Helvetica-Bold', fontSize=7.5,
                                         textColor=self.RED, alignment=TA_CENTER, leading=9),
        }

    # ──────────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _pct(v, dec=2, sign=True, na='—'):
        if v is None: return na
        s = '+' if sign and v > 0 else ''
        return f'{s}{v:.{dec}f}%'

    def _ks(self, v, size='big'):
        _map = {
            'big': ('kpi_big',  'kpi_pos',  'kpi_neg'),
            'med': ('kpi_med',  'kpi_mpos', 'kpi_mneg'),
            'sm':  ('kpi_sm',   'kpi_spos', 'kpi_sneg'),
        }
        neu, pos, neg = _map.get(size, _map['big'])
        S = self._styles()
        if v is None: return S[neu]
        return S[pos] if v > 0 else (S[neg] if v < 0 else S[neu])

    def _kpi_cell(self, val_str, lbl_str, val_style):
        S = self._styles()
        return [Paragraph(val_str, val_style), Spacer(1, 2), Paragraph(lbl_str, S['kpi_lbl'])]

    def _section_line(self, label, cw):
        S = self._styles()
        return [
            Paragraph(label.upper(), S['section_hd']),
            HRFlowable(width=cw, thickness=0.8, color=self.NAVY, spaceAfter=4, spaceBefore=2),
        ]

    # ──────────────────────────────────────────────────────────────────
    # HEADER PAGE (réutilisable)
    # ──────────────────────────────────────────────────────────────────
    def _page_header(self, cw, date_fr, subtitle, etf_name):
        S = self._styles()
        logo_cell = ''
        if os.path.exists(self.LOGO):
            logo_cell = Image(self.LOGO, width=4.5 * cm, height=1.35 * cm,
                              kind='proportional')

        right_block = [
            Paragraph(etf_name or 'CGF BRVM30 ETF', S['fund_name']),
            Spacer(1, 2),
            Paragraph('BULLETIN QUOTIDIEN DE VALEUR LIQUIDATIVE', S['doc_type']),
            Spacer(1, 2),
            Paragraph(f'{subtitle} — {date_fr}', S['date_main']),
        ]
        hdr = Table(
            [[logo_cell, right_block]],
            colWidths=[5 * cm, cw - 5 * cm],
        )
        hdr.setStyle(TableStyle([
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('LEFTPADDING',   (0, 0), (-1, -1), 0),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ]))
        return [
            hdr,
            Spacer(1, 4),
            HRFlowable(width=cw, thickness=1.5, color=self.NAVY, spaceAfter=0),
            HRFlowable(width=cw, thickness=1.5, color=self.GOLD, spaceAfter=6),
        ]

    # ──────────────────────────────────────────────────────────────────
    # GENERATE
    # ──────────────────────────────────────────────────────────────────
    def generate(self, report_date: str = None, force: bool = False) -> str:
        os.makedirs(self.PDFS_DIR, exist_ok=True)
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')

        pdf_path = os.path.join(self.PDFS_DIR, f'rapport_journalier_{report_date}.pdf')
        if os.path.exists(pdf_path) and not force:
            print(f"Rapport déjà existant : {pdf_path}")
            return pdf_path

        print("Chargement des données...")
        nl     = self._load('nav_latest.json')          or {}
        intra  = self._load('intraday_nav.json')         or {}
        ih     = self._load('nav_intraday_history.json') or {}
        launch = self._load('launch_state.json')         or {}

        # ── Données de base ──────────────────────────────────────────
        par           = float(launch.get('par_fcfa', 100000))
        launch_date   = launch.get('launch_date', report_date)
        n_parts       = int(launch.get('n_parts', 0))
        etf_name      = nl.get('etf_name', 'CGF BRVM30 ETF')

        today_snaps   = ih.get(report_date) or intra.get('snapshots', [])
        last_snap     = today_snaps[-1] if today_snaps else {}

        vl_cloture    = float(last_snap.get('vl_live_fcfa') or last_snap.get('vl_fcfa') or
                              last_snap.get('vl') or par)
        _c1d          = last_snap.get('change_1d_pct')
        var_jour      = _c1d if _c1d is not None else last_snap.get('change_day_pct')
        aum           = float(last_snap.get('aum_mfcfa') or nl.get('aum_mfcfa') or 0)
        perf_launch   = last_snap.get('perf_since_launch')
        n_prices      = int(last_snap.get('n_prices') or nl.get('n_live_prices') or 0)
        heure_cloture = last_snap.get('time', '—')
        brvm30_live   = last_snap.get('brvm30_official')

        # ── BRVM30 officiel ──────────────────────────────────────────
        brvm30_hist      = self._load('brvm30_index_history.json') or {}
        brvm30_at_launch = float(launch.get('brvm30_index_at_launch') or
                                 (brvm30_hist.get(launch_date) or 0)) or None
        brvm30_now       = brvm30_live
        if not brvm30_now and brvm30_hist:
            brvm30_now = brvm30_hist.get(report_date) or float(brvm30_hist[max(brvm30_hist.keys())])
        perf_idx = (float(brvm30_now) / brvm30_at_launch - 1) * 100 \
                   if brvm30_now and brvm30_at_launch else None

        # ── Séries closes ETF vs BRVM30 ──────────────────────────────
        launch_ts  = pd.Timestamp(launch_date)
        closes_etf = {}
        closes_idx = {}
        for d, pts in ih.items():
            if pts and pd.Timestamp(d) >= launch_ts:
                lp = pts[-1]
                vl = lp.get('vl_fcfa') or lp.get('vl')
                bv = lp.get('brvm30_official')
                if vl: closes_etf[d] = float(vl)
                if bv:
                    closes_idx[d] = float(bv)
                elif d in brvm30_hist:
                    closes_idx[d] = float(brvm30_hist[d])

        te = td = None
        etf_cl    = pd.Series(closes_etf).sort_index()
        idx_cl    = pd.Series(closes_idx).sort_index()
        n_seances = len(etf_cl)
        if len(etf_cl) >= 2 and len(idx_cl) >= 2:
            ret_etf = etf_cl.pct_change().dropna()
            ret_idx = idx_cl.pct_change().dropna()
            common  = ret_etf.index.intersection(ret_idx.index)
            if len(common) >= 1:
                active = ret_etf.loc[common] - ret_idx.loc[common]
                te = (float(active.std() * np.sqrt(252) * 100) if len(common) >= 2
                      else float(abs(active.iloc[0]) * np.sqrt(252) * 100))
            if brvm30_at_launch and not etf_cl.empty and not idx_cl.empty:
                etf_cum = etf_cl.iloc[-1] / par
                idx_cum = idx_cl.iloc[-1] / brvm30_at_launch
                td = (etf_cum / idx_cum - 1) * 100

        # ── Métriques de risque live ──────────────────────────────────
        live_cagr = live_sharpe = live_maxdd = None
        if len(etf_cl) >= 2:
            ret_live  = etf_cl.pct_change().dropna()
            total_ret = etf_cl.iloc[-1] / etf_cl.iloc[0] - 1
            live_cagr = ((1 + total_ret) ** (252 / len(etf_cl)) - 1) * 100
            if ret_live.std() > 0:
                live_sharpe = float((ret_live.mean() - 0.035 / 252) /
                                     ret_live.std() * (252 ** 0.5))
            roll_max   = etf_cl.cummax()
            live_maxdd = float(((etf_cl - roll_max) / roll_max * 100).min())
        elif len(etf_cl) == 1:
            live_maxdd = 0.0

        # Métriques backtest (depuis nav_latest — plus représentatives)
        vol_bt     = nl.get('vol_ann_pct')
        sharpe_bt  = nl.get('sharpe_ratio')
        maxdd_bt   = nl.get('max_drawdown_pct')
        perf_ytd   = nl.get('perf_ytd')
        perf_3m    = nl.get('perf_3m')

        # ── Rebalancement ─────────────────────────────────────────────
        last_rebal     = nl.get('last_rebal_date')
        next_rebal_str = '—'
        days_rebal     = None
        rebal_progress = None
        if last_rebal:
            try:
                from dateutil.relativedelta import relativedelta
                lr = pd.Timestamp(last_rebal)
                nr = lr + relativedelta(months=3)
                while nr.weekday() >= 5:
                    nr += pd.Timedelta(days=1)
                next_rebal_str = _date_fr_short(nr)
                days_rebal = (nr - pd.Timestamp(report_date).normalize()).days
                total_days = (nr - lr).days
                rebal_progress = max(0, min(100, int((1 - days_rebal / total_days) * 100))) \
                                 if total_days > 0 else None
            except Exception:
                pass

        jours_lct = (pd.Timestamp(report_date) - pd.Timestamp(launch_date)).days
        non_repr  = n_seances < 30

        print("Scraping Sika pour les cours de clôture...")
        sika   = self._scrape_sika_variations()
        basket = nl.get('basket', [])

        print("Génération du PDF...")
        S  = self._styles()
        cw = self.PAGE_W - 2 * self.MARGIN

        doc = SimpleDocTemplate(
            pdf_path, pagesize=A4,
            leftMargin=self.MARGIN, rightMargin=self.MARGIN,
            topMargin=1.0 * cm, bottomMargin=1.0 * cm,
            title=f'Bulletin VL — {etf_name} — {report_date}',
            author='CGF Gestion',
        )

        story = []
        date_fr = _date_fr(report_date)

        # ══════════════════════════════════════════════════════════════
        # PAGE 1 — BULLETIN DE VALEUR LIQUIDATIVE
        # ══════════════════════════════════════════════════════════════
        story += self._page_header(cw, date_fr, f'Séance J+{jours_lct + 1}', etf_name)

        # ── Bloc identification fonds ──────────────────────────────────
        id_data = [
            ['Gestionnaire', 'CGF Gestion',
             'Type de fonds', 'OPCVM indiciel coté (ETF)'],
            ['Marché de cotation', 'BRVM — Bourse Régionale des Valeurs Mobilières',
             'Indice de référence', 'BRVM30 Price Return'],
            ['Prix d\'émission', f'{par:,.0f} FCFA / part',
             'Date de lancement', _date_fr_short(launch_date)],
        ]
        id_col = [cw * 0.14, cw * 0.34, cw * 0.18, cw * 0.34]
        id_tbl = Table(id_data, colWidths=id_col)
        id_tbl.setStyle(TableStyle([
            ('FONTNAME',      (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME',      (0, 0), (0, -1),  'Helvetica-Bold'),
            ('FONTNAME',      (2, 0), (2, -1),  'Helvetica-Bold'),
            ('FONTSIZE',      (0, 0), (-1, -1), 7.5),
            ('TEXTCOLOR',     (0, 0), (0, -1),  self.NAVY),
            ('TEXTCOLOR',     (2, 0), (2, -1),  self.NAVY),
            ('TEXTCOLOR',     (1, 0), (1, -1),  self.BLACK),
            ('TEXTCOLOR',     (3, 0), (3, -1),  self.BLACK),
            ('BACKGROUND',    (0, 0), (-1, -1), self.LGRAY),
            ('GRID',          (0, 0), (-1, -1), 0.3, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING',   (0, 0), (-1, -1), 7),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(id_tbl)
        story.append(Spacer(1, 8))

        # ── VL officielle (bloc principal) ─────────────────────────────
        story += self._section_line('Valeur Liquidative Officielle de Clôture', cw)

        vl_vs = S['kpi_pos'] if (var_jour or 0) > 0 else (S['kpi_neg'] if (var_jour or 0) < 0 else S['kpi_big'])
        vl_row = [
            self._kpi_cell(f'{vl_cloture:,.0f}', 'Valeur liquidative (FCFA/part)',
                           S['kpi_big']),
            self._kpi_cell(self._pct(var_jour), 'Variation journalière',
                           self._ks(var_jour)),
            self._kpi_cell(f'{aum:,.1f} M', 'Actif net indicatif (FCFA)',
                           S['kpi_big']),
            self._kpi_cell(f'{n_parts:,}', 'Parts en circulation',
                           S['kpi_big']),
            self._kpi_cell(f'{n_prices}/27', 'Cours BRVM disponibles',
                           S['kpi_big'] if n_prices >= 25 else S['kpi_mneg']),
        ]
        vl_tbl = Table([vl_row], colWidths=[cw / 5] * 5)
        vl_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.LGRAY),
            ('BOX',           (0, 0), (-1, -1), 0.8, self.NBORD),
            ('LINEAFTER',     (0, 0), (3, 0),   0.5, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(vl_tbl)
        story.append(Spacer(1, 8))

        # ── Performance depuis le lancement ───────────────────────────
        story += self._section_line(f'Performance depuis le lancement ({_date_fr_short(launch_date)})', cw)

        perf_td_str = (f'{td:+.3f}% ({td * 100:+.0f} bps)' if td is not None else '—')
        perf_row = [
            self._kpi_cell(self._pct(perf_launch), 'ETF CGF BRVM30',
                           self._ks(perf_launch, 'med')),
            self._kpi_cell(self._pct(perf_idx), 'BRVM30 (indice officiel)',
                           self._ks(perf_idx, 'med')),
            self._kpi_cell(perf_td_str, 'Tracking Difference (TD)',
                           self._ks(td, 'med')),
            self._kpi_cell(f'{brvm30_now:.2f}' if brvm30_now else '—',
                           'BRVM30 niveau actuel',
                           S['kpi_med']),
            self._kpi_cell(_date_fr_short(nl.get('calc_date', report_date)),
                           'Date de référence NAV',
                           S['kpi_med']),
        ]
        perf_tbl = Table([perf_row], colWidths=[cw / 5] * 5)
        perf_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (1, 0),  self.LGOLD),
            ('BACKGROUND',    (2, 0), (-1, 0), self.WHITE),
            ('BOX',           (0, 0), (-1, -1), 0.8, self.NBORD),
            ('LINEAFTER',     (0, 0), (3, 0),  0.5, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(perf_tbl)
        story.append(Spacer(1, 8))

        # ── Indicateurs de réplication et de risque ────────────────────
        story += self._section_line(
            'Indicateurs de réplication et de risque' +
            (' — données < 30 séances, non représentatifs*' if non_repr else ''),
            cw,
        )

        star = '*' if non_repr else ''
        risk_row = [
            self._kpi_cell(
                f'{te:.2f}%' if te is not None else '—',
                'Tracking Error annualisée', S['kpi_sm']),
            self._kpi_cell(
                f'{live_sharpe:.2f}{star}' if live_sharpe is not None else '—',
                'Ratio de Sharpe (rf 3,5%)', S['kpi_sm']),
            self._kpi_cell(
                self._pct(live_cagr) + star if live_cagr else '—',
                'CAGR annualisé',
                self._ks(live_cagr, 'sm')),
            self._kpi_cell(
                self._pct(live_maxdd) if live_maxdd is not None else '—',
                'Maximum Drawdown',
                self._ks(live_maxdd, 'sm')),
            self._kpi_cell(
                f'{n_seances}',
                'Séances enregistrées', S['kpi_sm']),
            self._kpi_cell(
                heure_cloture + ' UTC',
                'Heure dernier iNAV', S['kpi_sm']),
        ]
        risk_tbl = Table([risk_row], colWidths=[cw / 6] * 6)
        risk_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.WHITE),
            ('BOX',           (0, 0), (-1, -1), 0.8, self.NBORD),
            ('LINEAFTER',     (0, 0), (4, 0),   0.5, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(risk_tbl)

        if non_repr:
            story.append(Spacer(1, 2))
            story.append(Paragraph(
                '* Données insuffisantes (< 30 séances) — '
                'TE = σ(rendements actifs quotidiens) × √252 · '
                'TD = (perf ETF / perf BRVM30) − 1 · '
                'Sharpe = (r̄ − rf) / σ × √252 avec rf = 3,5 %/an (taux directeur BCEAO)',
                S['note'],
            ))

        story.append(Spacer(1, 8))

        # ── Graphique iNAV intraday ────────────────────────────────────
        story += self._section_line(
            f'iNAV intraday — {len(today_snaps)} points de valorisation' if today_snaps else 'iNAV intraday',
            cw,
        )
        if today_snaps:
            buf1 = self._chart_intraday(today_snaps, par)
            story.append(Image(buf1, width=cw, height=cw * 3.2 / 13))
        else:
            story.append(Paragraph('Aucune donnée iNAV intraday pour cette séance.', S['note']))

        story.append(Spacer(1, 8))

        # ── Rebalancement ─────────────────────────────────────────────
        story += self._section_line('Informations de rebalancement', cw)

        reb_row = [[
            Paragraph('Dernier rebalancement', S['kpi_lbl']),
            Paragraph('Prochain rebalancement (estimé)', S['kpi_lbl']),
            Paragraph('Jours restants', S['kpi_lbl']),
            Paragraph('Avancement de la période', S['kpi_lbl']),
            Paragraph('Jours depuis lancement', S['kpi_lbl']),
        ], [
            Paragraph(_date_fr_short(last_rebal) if last_rebal else '—', S['kpi_sm']),
            Paragraph(next_rebal_str, S['kpi_sm']),
            Paragraph(
                f'{days_rebal}j',
                S['kpi_sneg'] if days_rebal is not None and days_rebal <= 10 else S['kpi_sm'],
            ) if days_rebal is not None else Paragraph('—', S['kpi_sm']),
            Paragraph(f'{rebal_progress}%' if rebal_progress is not None else '—', S['kpi_sm']),
            Paragraph(f'{jours_lct + 1}j', S['kpi_sm']),
        ]]
        reb_tbl = Table(reb_row, colWidths=[cw / 5] * 5)
        reb_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.LGRAY),
            ('BOX',           (0, 0), (-1, -1), 0.8, self.NBORD),
            ('LINEAFTER',     (0, 0), (3, -1),  0.5, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(reb_tbl)

        # ── Footer page 1 ─────────────────────────────────────────────
        story.append(Spacer(1, 6))
        story.append(HRFlowable(width=cw, thickness=0.4, color=self.NBORD, spaceAfter=3))
        story.append(Paragraph(
            f'Document réglementaire · CGF Gestion — Gestionnaire d\'actifs agréé CREPMF · '
            f'Rapport généré le {datetime.now().strftime("%d/%m/%Y à %H:%M")} UTC · '
            f'Source cours : sikafinance.com · Les performances passées ne préjugent pas '
            f'des performances futures.',
            S['note'],
        ))

        # ══════════════════════════════════════════════════════════════
        # PAGE 2 — COMPOSITION DU PORTEFEUILLE
        # ══════════════════════════════════════════════════════════════
        story.append(PageBreak())
        story += self._page_header(cw, date_fr, 'Composition du portefeuille', etf_name)

        # ── Performance relative base 100 ──────────────────────────────
        n_days = sum(1 for d, pts in ih.items() if pts and pd.Timestamp(d) >= launch_ts)
        story += self._section_line(
            f'Performance relative depuis le lancement — base 100 au {_date_fr_short(launch_date)} · {n_days} séance(s)',
            cw,
        )
        if n_days >= 1:
            buf2 = self._chart_base100(ih, launch_date, brvm30_hist, brvm30_at_launch, par)
            story.append(Image(buf2, width=cw, height=cw * 2.6 / 13))
        else:
            story.append(Paragraph('Données insuffisantes.', S['note']))

        story.append(Spacer(1, 6))

        # ── Tableau portefeuille ───────────────────────────────────────
        story += self._section_line(
            f'Panier BRVM30 — {len(basket)} titres · Données de clôture : {nl.get("calc_date","—")}',
            cw,
        )

        col_w = [cw * x for x in [0.095, 0.085, 0.125, 0.095, 0.125, 0.095, 0.38]]
        hdr_cells = ['Ticker', 'Poids', 'Clôture J-1', 'Var. J', 'Cours live', 'Qlté', 'Valeur (M FCFA)']
        tbl_data   = [hdr_cells]
        variations = []

        for r in basket:
            ticker = r['ticker'].upper()
            sk     = sika.get(ticker, {})
            var_j  = sk.get('variation') if isinstance(sk, dict) else None
            cours  = sk.get('dernier')   if isinstance(sk, dict) else None
            qte    = r.get('quantite') or r.get('qty') or '—'
            variations.append(var_j)
            tbl_data.append([
                ticker,
                f"{r['poids_pct']:.2f}%",
                f"{int(r['dernier_prix']):,}" if r.get('dernier_prix') else '—',
                f'{var_j:+.2f}%' if var_j is not None else '—',
                f"{int(cours):,}" if cours else '—',
                f"{int(qte):,}" if isinstance(qte, (int, float)) else str(qte),
                f"{r['pv_mfcfa']:.2f}",
            ])

        port_tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
        s_cmds = [
            ('BACKGROUND',    (0, 0), (-1, 0), self.NAVY),
            ('TEXTCOLOR',     (0, 0), (-1, 0), self.WHITE),
            ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('LINEBELOW',     (0, 0), (-1, 0), 1.5, self.GOLD),
            ('FONTSIZE',      (0, 0), (-1, -1), 7.5),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('BOX',           (0, 0), (-1, -1), 0.8, self.NBORD),
            ('GRID',          (0, 1), (-1, -1), 0.3, self.BORDER),
        ]
        for i in range(1, len(tbl_data)):
            if i % 2 == 0:
                s_cmds.append(('BACKGROUND', (0, i), (-1, i), self.LGRAY))
            v = variations[i - 1]
            if v is not None and v > 0:
                s_cmds.append(('TEXTCOLOR', (3, i), (3, i), self.GREEN))
                s_cmds.append(('FONTNAME',  (3, i), (3, i), 'Helvetica-Bold'))
            elif v is not None and v < 0:
                s_cmds.append(('TEXTCOLOR', (3, i), (3, i), self.RED))
                s_cmds.append(('FONTNAME',  (3, i), (3, i), 'Helvetica-Bold'))
        port_tbl.setStyle(TableStyle(s_cmds))
        story.append(port_tbl)
        story.append(Spacer(1, 8))

        # ── Top mouvements ────────────────────────────────────────────
        basket_var = [
            (r['ticker'].upper(), r['poids_pct'],
             sika.get(r['ticker'].upper(), {}).get('variation'))
            for r in basket
            if isinstance(sika.get(r['ticker'].upper(), {}), dict)
            and sika.get(r['ticker'].upper(), {}).get('variation') is not None
        ]

        if basket_var:
            top5    = sorted(basket_var, key=lambda x: x[2], reverse=True)[:5]
            bottom5 = sorted(basket_var, key=lambda x: x[2])[:5]

            story += self._section_line('Top mouvements du jour', cw)

            tb_headers = [
                [Paragraph('HAUSSES', ParagraphStyle('th_g', fontName='Helvetica-Bold',
                           fontSize=7.5, textColor=self.WHITE, alignment=TA_CENTER)),
                 '', '',
                 Paragraph('BAISSES', ParagraphStyle('th_r', fontName='Helvetica-Bold',
                           fontSize=7.5, textColor=self.WHITE, alignment=TA_CENTER)),
                 '', ''],
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
                tb_headers.append(row)

            mv_tbl = Table(tb_headers, colWidths=[cw / 6] * 6, repeatRows=2)
            mv_s = [
                ('SPAN', (0, 0), (2, 0)), ('SPAN', (3, 0), (5, 0)),
                ('BACKGROUND', (0, 0), (2, 0), self.GREEN),
                ('BACKGROUND', (3, 0), (5, 0), self.RED),
                ('BACKGROUND', (0, 1), (2, 1), colors.HexColor('#dcfce7')),
                ('BACKGROUND', (3, 1), (5, 1), colors.HexColor('#fee2e2')),
                ('FONTNAME', (0, 0), (-1, 1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 8),
                ('ALIGN',    (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN',   (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING',    (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('BOX',  (0, 0), (-1, -1), 0.8, self.NBORD),
                ('GRID', (0, 0), (-1, -1), 0.3, self.BORDER),
                ('TEXTCOLOR', (0, 1), (2, 1), self.GREEN),
                ('TEXTCOLOR', (3, 1), (5, 1), self.RED),
            ]
            for i in range(2, len(tb_headers)):
                if i - 2 < len(top5):
                    mv_s += [('TEXTCOLOR', (2, i), (2, i), self.GREEN),
                              ('FONTNAME',  (2, i), (2, i), 'Helvetica-Bold')]
                if i - 2 < len(bottom5):
                    mv_s += [('TEXTCOLOR', (5, i), (5, i), self.RED),
                              ('FONTNAME',  (5, i), (5, i), 'Helvetica-Bold')]
            mv_tbl.setStyle(TableStyle(mv_s))
            story.append(mv_tbl)

        # ── Footer page 2 ─────────────────────────────────────────────
        story.append(Spacer(1, 8))
        story.append(HRFlowable(width=cw, thickness=0.4, color=self.NBORD, spaceAfter=3))
        story.append(Paragraph(
            f'Document réglementaire à usage interne — CGF Gestion · '
            f'Gestionnaire d\'actifs agréé CREPMF · '
            f'Rapport généré le {datetime.now().strftime("%d/%m/%Y à %H:%M")} UTC · '
            f'Données de marché : sikafinance.com · '
            f'Les performances passées ne préjugent pas des performances futures. '
            f'Ce document ne constitue pas un conseil en investissement.',
            S['note'],
        ))

        doc.build(story)
        print(f'Rapport généré : {pdf_path}')
        return pdf_path

    def run(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--date',  default=None)
        parser.add_argument('--force', action='store_true')
        args = parser.parse_args()
        self.generate(report_date=args.date, force=args.force)


if __name__ == '__main__':
    ReportGenerator().run()
