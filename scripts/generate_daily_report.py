"""
generate_daily_report.py — Rapport journalier CGF BRVM30 ETF
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
    Image, HRFlowable, PageBreak,
)

from base import BaseScript

JOURS_FR = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi', 'Dimanche']
MOIS_FR  = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin',
            'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']

def _date_fr(date_str):
    t = pd.Timestamp(date_str)
    return f"{JOURS_FR[t.weekday()]} {t.day} {MOIS_FR[t.month - 1]} {t.year}"


class ReportGenerator(BaseScript):
    DARK   = colors.HexColor("#0c1a2e")
    NAVY   = colors.HexColor("#1e3a5f")
    BLUE   = colors.HexColor("#2563eb")
    GOLD   = colors.HexColor("#b89b3f")
    LGOLD  = colors.HexColor("#fdf8ee")
    LNAVY  = colors.HexColor("#eef2f7")
    GREEN  = colors.HexColor("#16a34a")
    LGREEN = colors.HexColor("#dcfce7")
    RED    = colors.HexColor("#dc2626")
    LRED   = colors.HexColor("#fee2e2")
    GRAY   = colors.HexColor("#64748b")
    BORDER = colors.HexColor("#d1d5db")
    ALTROW = colors.HexColor("#f8fafc")
    WHITE  = colors.white
    BLACK  = colors.HexColor("#1e293b")

    def __init__(self):
        super().__init__()
        self.PAGE_W, self.PAGE_H = A4
        self.MARGIN   = 1.6 * cm
        self.PDFS_DIR = os.path.join(self.data_dir, 'pdfs')

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
                    return c.get_text(strip=True).replace('\xa0', '').replace(' ', '').replace(',', '.').replace('%', '')
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

    def _chart_intraday(self, snapshots, par, report_date):
        fig, ax = plt.subplots(figsize=(13, 3.0))
        fig.patch.set_facecolor('#ffffff')
        ax.set_facecolor('#fafbfc')

        times  = [s['time'] for s in snapshots]
        vl_pts = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0)
                  for s in snapshots]

        xs = range(len(times))
        ax.plot(xs, vl_pts, color='#1e3a5f', linewidth=2.0, zorder=3)
        ax.fill_between(xs, vl_pts, min(vl_pts) * 0.9995, alpha=0.08, color='#1e3a5f')
        ax.axhline(y=par, color='#b89b3f', linestyle='--', linewidth=1.2, alpha=0.9,
                   label=f"Émission {par:,.0f} FCFA")

        step = max(1, len(times) // 8)
        ax.set_xticks(list(xs)[::step])
        ax.set_xticklabels(times[::step], rotation=45, ha='right', fontsize=7.5)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:,.0f}'))
        ax.set_ylabel('FCFA / part', fontsize=8, color='#64748b')
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

    def _chart_base100(self, intra_hist, launch_date, brvm30_hist, brvm30_at_launch, par):
        fig, ax = plt.subplots(figsize=(13, 2.8))
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
        xs = range(len(etf_s))

        ax.plot(xs, etf_s.values, color='#1e3a5f', linewidth=2.5,
                marker='o', markersize=6, label='CGF BRVM30 ETF', zorder=3)
        if not idx_s.empty:
            idx_aligned = idx_s.reindex(etf_s.index)
            ax.plot(xs, idx_aligned.values, color='#b89b3f', linewidth=2.0,
                    linestyle='--', marker='s', markersize=5, label='BRVM30', zorder=2)
        ax.axhline(y=100, color='#94a3b8', linestyle=':', linewidth=1, alpha=0.7)

        ax.set_xticks(list(xs))
        ax.set_xticklabels(labels, rotation=0, ha='center', fontsize=9)
        ax.set_ylabel('Base 100', fontsize=8.5, color='#64748b')
        ax.tick_params(colors='#64748b', labelsize=8)
        ax.legend(fontsize=9, framealpha=0.5, loc='upper left')
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

    def _styles(self):
        return {
            'hdr_title': ParagraphStyle('hdr_t', fontName='Helvetica-Bold', fontSize=22,
                                        textColor=self.WHITE, leading=26),
            'hdr_sub':   ParagraphStyle('hdr_s', fontName='Helvetica', fontSize=9,
                                        textColor=colors.HexColor('#a0b0c8'), leading=12),
            'hdr_date':  ParagraphStyle('hdr_d', fontName='Helvetica-Bold', fontSize=12,
                                        textColor=self.WHITE, alignment=TA_RIGHT, leading=15),
            'hdr_rpt':   ParagraphStyle('hdr_r', fontName='Helvetica', fontSize=8,
                                        textColor=colors.HexColor('#a0b0c8'),
                                        alignment=TA_RIGHT, leading=11),
            'section':   ParagraphStyle('sec', fontName='Helvetica-Bold', fontSize=8,
                                        textColor=self.GRAY, spaceBefore=0, spaceAfter=0,
                                        leading=10),
            'kpi_val':   ParagraphStyle('kv', fontName='Helvetica-Bold', fontSize=15,
                                        textColor=self.DARK, alignment=TA_CENTER, leading=18),
            'kpi_pos':   ParagraphStyle('kp', fontName='Helvetica-Bold', fontSize=15,
                                        textColor=self.GREEN, alignment=TA_CENTER, leading=18),
            'kpi_neg':   ParagraphStyle('kn', fontName='Helvetica-Bold', fontSize=15,
                                        textColor=self.RED, alignment=TA_CENTER, leading=18),
            'kpi_lbl':   ParagraphStyle('kl', fontName='Helvetica', fontSize=6.5,
                                        textColor=self.GRAY, alignment=TA_CENTER, leading=8),
            'skpi_val':  ParagraphStyle('skv', fontName='Helvetica-Bold', fontSize=12,
                                        textColor=self.DARK, alignment=TA_CENTER, leading=15),
            'skpi_pos':  ParagraphStyle('skp', fontName='Helvetica-Bold', fontSize=12,
                                        textColor=self.GREEN, alignment=TA_CENTER, leading=15),
            'skpi_neg':  ParagraphStyle('skn', fontName='Helvetica-Bold', fontSize=12,
                                        textColor=self.RED, alignment=TA_CENTER, leading=15),
            'skpi_lbl':  ParagraphStyle('skl', fontName='Helvetica', fontSize=6,
                                        textColor=self.GRAY, alignment=TA_CENTER, leading=8),
            'body':      ParagraphStyle('body', fontName='Helvetica', fontSize=8.5,
                                        textColor=self.BLACK, leading=12),
            'note':      ParagraphStyle('note', fontName='Helvetica-Oblique', fontSize=7,
                                        textColor=self.GRAY, leading=9.5),
            'bold_s':    ParagraphStyle('bs', fontName='Helvetica-Bold', fontSize=8,
                                        textColor=self.DARK, leading=10),
        }

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

        par           = float(launch.get('par_fcfa', 100000))
        nav_at_launch = float(launch.get('nav_index_at_launch', 0))
        launch_date   = launch.get('launch_date', report_date)
        n_parts       = int(launch.get('n_parts', 0))

        today_snaps   = ih.get(report_date) or intra.get('snapshots', [])
        last_snap     = today_snaps[-1] if today_snaps else {}

        vl_cloture    = float(last_snap.get('vl_live_fcfa') or last_snap.get('vl_fcfa') or
                              last_snap.get('vl') or par)
        _c1d          = last_snap.get('change_1d_pct')
        var_jour      = _c1d if _c1d is not None else last_snap.get('change_day_pct')
        aum           = float(last_snap.get('aum_mfcfa') or nl.get('aum_mfcfa') or 0)
        perf_launch   = last_snap.get('perf_since_launch')
        nav_indice    = float(last_snap.get('nav_indice') or nl.get('nav_indice') or 0)
        n_prices      = int(last_snap.get('n_prices') or 0)
        heure_cloture = last_snap.get('time', '—')

        brvm30_hist      = self._load('brvm30_index_history.json') or {}
        brvm30_at_launch = float(brvm30_hist[launch_date]) if launch_date in brvm30_hist else None
        brvm30_now       = last_snap.get('brvm30_official')
        if not brvm30_now and brvm30_hist:
            brvm30_now = brvm30_hist.get(report_date) or float(brvm30_hist[max(brvm30_hist.keys())])
        perf_idx = (float(brvm30_now) / brvm30_at_launch - 1) * 100 if brvm30_now and brvm30_at_launch else None

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

        live_cagr = live_sharpe = live_maxdd = None
        if len(etf_cl) >= 2:
            ret_live  = etf_cl.pct_change().dropna()
            total_ret = etf_cl.iloc[-1] / etf_cl.iloc[0] - 1
            live_cagr = ((1 + total_ret) ** (252 / len(etf_cl)) - 1) * 100
            if ret_live.std() > 0:
                live_sharpe = float((ret_live.mean() - 0.035 / 252) / ret_live.std() * (252 ** 0.5))
            roll_max   = etf_cl.cummax()
            live_maxdd = float(((etf_cl - roll_max) / roll_max * 100).min())
        elif len(etf_cl) == 1:
            live_maxdd = 0.0

        last_rebal     = nl.get('last_rebal_date')
        next_rebal_str = '—'
        days_rebal     = None
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
        non_repr  = n_seances is not None and n_seances < 30

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
            title=f'CGF BRVM30 ETF — Rapport journalier {report_date}',
            author='CGF Bourse',
        )

        story = []
        date_fr = _date_fr(report_date)

        # ══════════════════════════════════════════════════════════════
        # HEADER
        # ══════════════════════════════════════════════════════════════
        hdr = Table(
            [[Paragraph('CGF BRVM30 ETF', S['hdr_title']),
              [Paragraph('RAPPORT JOURNALIER', S['hdr_rpt']),
               Paragraph(date_fr, S['hdr_date'])]]],
            colWidths=[cw * 0.55, cw * 0.45],
        )
        hdr.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.DARK),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 14),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
            ('LEFTPADDING',   (0, 0), (0, 0),   18),
            ('RIGHTPADDING',  (1, 0), (1, 0),   18),
        ]))
        story.append(hdr)
        story.append(HRFlowable(width=cw, thickness=3, color=self.GOLD, spaceAfter=0))

        info_bar = Table(
            [[Paragraph(
                f'Séance J+{jours_lct + 1} depuis le lancement · '
                f'{n_parts:,} parts en circulation · '
                f'Prix d\'émission : {par:,.0f} FCFA · '
                f'iNAV clôture : {heure_cloture} UTC · '
                f'{n_prices} cours sur 27',
                S['note'],
            )]],
            colWidths=[cw],
        )
        info_bar.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.LNAVY),
            ('TOPPADDING',    (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING',   (0, 0), (-1, -1), 10),
        ]))
        story.append(info_bar)
        story.append(Spacer(1, 8))

        # ══════════════════════════════════════════════════════════════
        # HELPERS KPI
        # ══════════════════════════════════════════════════════════════
        def _fmt(v, dec=2, sign=True):
            if v is None: return '—'
            s = '+' if sign and v > 0 else ''
            return f'{s}{v:.{dec}f}%'

        def _ks(v, pfx='kpi'):
            if v is None: return S[f'{pfx}_val']
            return S[f'{pfx}_pos'] if v > 0 else (S[f'{pfx}_neg'] if v < 0 else S[f'{pfx}_val'])

        def _kpi(val_p, lbl_p):
            return [val_p, Spacer(1, 3), lbl_p]

        # ══════════════════════════════════════════════════════════════
        # BANDE KPI 1 — CLÔTURE (5 cellules)
        # ══════════════════════════════════════════════════════════════
        row1 = [
            _kpi(Paragraph(f'{vl_cloture:,.0f} FCFA', S['kpi_val']),
                 Paragraph('VL de clôture', S['kpi_lbl'])),
            _kpi(Paragraph(_fmt(var_jour), _ks(var_jour)),
                 Paragraph('Variation du jour', S['kpi_lbl'])),
            _kpi(Paragraph(f'{aum:,.1f} M FCFA', S['kpi_val']),
                 Paragraph('AUM indicatif', S['kpi_lbl'])),
            _kpi(Paragraph(_fmt(perf_launch), _ks(perf_launch)),
                 Paragraph(f'Perf. ETF / lancement', S['kpi_lbl'])),
            _kpi(Paragraph(_fmt(perf_idx), _ks(perf_idx)),
                 Paragraph('BRVM30 même période', S['kpi_lbl'])),
        ]
        kpi1 = Table([row1], colWidths=[cw / 5] * 5)
        kpi1.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.WHITE),
            ('BOX',           (0, 0), (-1, -1), 1, self.BORDER),
            ('LINEAFTER',     (0, 0), (3, 0),   0.5, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(kpi1)
        story.append(Spacer(1, 3))

        # ══════════════════════════════════════════════════════════════
        # BANDE KPI 2 — RÉPLICATION & RISQUE (6 cellules)
        # ══════════════════════════════════════════════════════════════
        star = '*' if non_repr else ''
        row2 = [
            _kpi(Paragraph(f'{te:.2f}%' if te is not None else '—', S['skpi_val']),
                 Paragraph('Tracking Error (TE)', S['skpi_lbl'])),
            _kpi(Paragraph(_fmt(td, 3), _ks(td, 'skpi')),
                 Paragraph('Tracking Diff. (TD)', S['skpi_lbl'])),
            _kpi(Paragraph(f'{live_sharpe:.2f}{star}' if live_sharpe is not None else '—',
                           S['skpi_val']),
                 Paragraph('Ratio de Sharpe', S['skpi_lbl'])),
            _kpi(Paragraph((_fmt(live_cagr) + star) if live_cagr is not None else '—',
                           _ks(live_cagr, 'skpi')),
                 Paragraph('CAGR annualisé', S['skpi_lbl'])),
            _kpi(Paragraph(_fmt(live_maxdd) if live_maxdd is not None else '—',
                           _ks(live_maxdd, 'skpi')),
                 Paragraph('Max Drawdown', S['skpi_lbl'])),
            _kpi(Paragraph(str(n_seances), S['skpi_val']),
                 Paragraph('Séances enregistrées', S['skpi_lbl'])),
        ]
        kpi2 = Table([row2], colWidths=[cw / 6] * 6)
        kpi2.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.LGOLD),
            ('BOX',           (0, 0), (-1, -1), 1, self.BORDER),
            ('LINEAFTER',     (0, 0), (4, 0),   0.5, self.BORDER),
            ('TOPPADDING',    (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        story.append(kpi2)
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            ('* Données < 30 séances, non représentatifs — ' if non_repr else '') +
            'TE = σ(rendements actifs) × √252 · TD = (perf ETF / perf BRVM30) − 1 · '
            'Sharpe avec rf = 3,5 % (BCEAO)',
            S['note'],
        ))
        story.append(Spacer(1, 8))

        # ══════════════════════════════════════════════════════════════
        # GRAPHIQUE INTRADAY
        # ══════════════════════════════════════════════════════════════
        story.append(HRFlowable(width=cw, thickness=1, color=self.GOLD, spaceAfter=4))
        story.append(Paragraph('iNAV INTRADAY', S['section']))
        story.append(Spacer(1, 4))
        if today_snaps:
            vl_vals = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0)
                       for s in today_snaps]
            buf1 = self._chart_intraday(today_snaps, par, report_date)
            story.append(Image(buf1, width=cw, height=cw * 3.0 / 13))
            story.append(Paragraph(
                f'{len(today_snaps)} points · Min : {min(vl_vals):,.0f} FCFA · '
                f'Max : {max(vl_vals):,.0f} FCFA · Clôture : {vl_cloture:,.0f} FCFA · '
                f'Ligne tirets = prix d\'émission',
                S['note'],
            ))
        else:
            story.append(Paragraph('Aucune donnée iNAV disponible pour cette séance.', S['note']))

        story.append(Spacer(1, 8))

        # ══════════════════════════════════════════════════════════════
        # BARRE REBALANCEMENT
        # ══════════════════════════════════════════════════════════════
        story.append(HRFlowable(width=cw, thickness=1, color=self.GOLD, spaceAfter=4))
        story.append(Paragraph('REBALANCEMENT', S['section']))
        story.append(Spacer(1, 4))
        rebal_data = [[
            Paragraph('Dernier rebalancement', S['skpi_lbl']),
            Paragraph('Prochain rebalancement', S['skpi_lbl']),
            Paragraph('Jours restants', S['skpi_lbl']),
            Paragraph('Jours depuis lancement', S['skpi_lbl']),
            Paragraph('Parts émises', S['skpi_lbl']),
        ], [
            Paragraph(last_rebal or '—', S['skpi_val']),
            Paragraph(next_rebal_str, S['skpi_val']),
            Paragraph(f'{days_rebal}j' if days_rebal is not None else '—',
                      S['skpi_neg'] if days_rebal is not None and days_rebal <= 10 else S['skpi_val']),
            Paragraph(f'{jours_lct}j', S['skpi_val']),
            Paragraph(f'{n_parts:,}', S['skpi_val']),
        ]]
        rebal_tbl = Table(rebal_data, colWidths=[cw / 5] * 5)
        rebal_tbl.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.LNAVY),
            ('TOPPADDING',    (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('BOX',           (0, 0), (-1, -1), 0.5, self.BORDER),
            ('LINEAFTER',     (0, 0), (3, -1), 0.5, self.BORDER),
        ]))
        story.append(rebal_tbl)

        # ══════════════════════════════════════════════════════════════
        # PAGE 2 — PERFORMANCE & PORTEFEUILLE
        # ══════════════════════════════════════════════════════════════
        story.append(PageBreak())

        # Header page 2
        p2hdr = Table(
            [[Paragraph('CGF BRVM30 ETF', S['hdr_title']),
              [Paragraph('PORTEFEUILLE & MARCHÉ', S['hdr_rpt']),
               Paragraph(date_fr, S['hdr_date'])]]],
            colWidths=[cw * 0.55, cw * 0.45],
        )
        p2hdr.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), self.DARK),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING',   (0, 0), (0, 0),   18),
            ('RIGHTPADDING',  (1, 0), (1, 0),   18),
        ]))
        story.append(p2hdr)
        story.append(HRFlowable(width=cw, thickness=3, color=self.GOLD, spaceAfter=6))

        # ── BASE 100 ──────────────────────────────────────────────────
        story.append(Paragraph('PERFORMANCE RELATIVE DEPUIS LE LANCEMENT — BASE 100', S['section']))
        story.append(Spacer(1, 4))
        n_days_data = sum(1 for d, pts in ih.items() if pts and pd.Timestamp(d) >= launch_ts)
        if n_days_data >= 1:
            buf2 = self._chart_base100(ih, launch_date, brvm30_hist, brvm30_at_launch, par)
            story.append(Image(buf2, width=cw, height=cw * 2.8 / 13))
            story.append(Paragraph(
                f'Base 100 = prix d\'émission ({par:,.0f} FCFA) au {launch_date} · '
                f'{n_days_data} séance(s) enregistrée(s) · '
                f'ETF (bleu) vs BRVM30 officiel (or)',
                S['note'],
            ))
        else:
            story.append(Paragraph('Données insuffisantes pour le graphique.', S['note']))

        story.append(Spacer(1, 8))
        story.append(HRFlowable(width=cw, thickness=1, color=self.GOLD, spaceAfter=4))

        # ── COMPOSITION ───────────────────────────────────────────────
        story.append(Paragraph(f'COMPOSITION DU PORTEFEUILLE — {len(basket)} TITRES', S['section']))
        story.append(Spacer(1, 2))
        story.append(Paragraph(
            f'Référence NAV : {nl.get("calc_date", "—")} · '
            f'AUM total : {aum:,.1f} M FCFA · '
            f'Cours live : sikafinance.com',
            S['note'],
        ))
        story.append(Spacer(1, 4))

        col_w = [cw * x for x in [0.10, 0.09, 0.13, 0.11, 0.13, 0.44]]
        headers_row = ['Ticker', 'Poids', 'Clôture J-1', 'Var. J', 'Cours live', 'Valeur (M FCFA)']
        tbl_data   = [headers_row]
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
            ('BACKGROUND',    (0, 0), (-1, 0), self.NAVY),
            ('TEXTCOLOR',     (0, 0), (-1, 0), self.WHITE),
            ('FONTNAME',      (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('LINEBELOW',     (0, 0), (-1, 0), 2, self.GOLD),
            ('FONTSIZE',      (0, 0), (-1, -1), 7.5),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('BOX',           (0, 0), (-1, -1), 1, self.BORDER),
            ('GRID',          (0, 1), (-1, -1), 0.4, self.BORDER),
        ]
        for i in range(1, len(tbl_data)):
            if i % 2 == 0:
                style_cmds.append(('BACKGROUND', (0, i), (-1, i), self.ALTROW))
            v = variations[i - 1]
            if v is not None and v > 0:
                style_cmds.append(('TEXTCOLOR', (3, i), (3, i), self.GREEN))
                style_cmds.append(('FONTNAME',  (3, i), (3, i), 'Helvetica-Bold'))
            elif v is not None and v < 0:
                style_cmds.append(('TEXTCOLOR', (3, i), (3, i), self.RED))
                style_cmds.append(('FONTNAME',  (3, i), (3, i), 'Helvetica-Bold'))
        port_tbl.setStyle(TableStyle(style_cmds))
        story.append(port_tbl)
        story.append(Spacer(1, 8))

        # ── TOP MOUVEMENTS ────────────────────────────────────────────
        basket_var = [
            (r['ticker'].upper(), r['poids_pct'],
             sika.get(r['ticker'].upper(), {}).get('variation'))
            for r in basket
            if isinstance(sika.get(r['ticker'].upper(), {}), dict)
            and sika.get(r['ticker'].upper(), {}).get('variation') is not None
        ]

        if basket_var:
            story.append(HRFlowable(width=cw, thickness=1, color=self.GOLD, spaceAfter=4))
            story.append(Paragraph('TOP MOUVEMENTS DU JOUR', S['section']))
            story.append(Spacer(1, 4))

            top5    = sorted(basket_var, key=lambda x: x[2], reverse=True)[:5]
            bottom5 = sorted(basket_var, key=lambda x: x[2])[:5]

            tb_data = [['TOP 5 HAUSSES', '', '', 'TOP 5 BAISSES', '', '']]
            tb_data.append(['Ticker', 'Poids', 'Variation', 'Ticker', 'Poids', 'Variation'])
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
                ('SPAN',       (0, 0), (2, 0)), ('SPAN', (3, 0), (5, 0)),
                ('BACKGROUND', (0, 0), (2, 0), self.GREEN),
                ('BACKGROUND', (3, 0), (5, 0), self.RED),
                ('TEXTCOLOR',  (0, 0), (5, 0), self.WHITE),
                ('FONTNAME',   (0, 0), (5, 0), 'Helvetica-Bold'),
                ('BACKGROUND', (0, 1), (2, 1), self.LGREEN),
                ('BACKGROUND', (3, 1), (5, 1), self.LRED),
                ('FONTNAME',   (0, 1), (-1, 1), 'Helvetica-Bold'),
                ('FONTSIZE',   (0, 0), (-1, -1), 8),
                ('ALIGN',      (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN',     (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING',    (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('BOX',  (0, 0), (-1, -1), 1, self.BORDER),
                ('GRID', (0, 0), (-1, -1), 0.5, self.BORDER),
            ]
            for i in range(2, len(tb_data)):
                if i - 2 < len(top5):
                    tb_style.append(('TEXTCOLOR', (2, i), (2, i), self.GREEN))
                    tb_style.append(('FONTNAME',  (2, i), (2, i), 'Helvetica-Bold'))
                if i - 2 < len(bottom5):
                    tb_style.append(('TEXTCOLOR', (5, i), (5, i), self.RED))
                    tb_style.append(('FONTNAME',  (5, i), (5, i), 'Helvetica-Bold'))
            tb_tbl.setStyle(TableStyle(tb_style))
            story.append(tb_tbl)

        # ── FOOTER ────────────────────────────────────────────────────
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width=cw, thickness=0.5, color=self.BORDER, spaceAfter=4))
        story.append(Paragraph(
            f'CGF BRVM30 ETF · Rapport généré le {datetime.now().strftime("%d/%m/%Y à %H:%M")} UTC · '
            f'Source données marché : sikafinance.com · Document à usage interne uniquement. '
            f'Les performances passées ne préjugent pas des performances futures.',
            S['note'],
        ))

        doc.build(story)
        print(f'Rapport généré : {pdf_path}')
        return pdf_path

    def run(self):
        import argparse
        parser = argparse.ArgumentParser(description='Rapport journalier CGF BRVM30 ETF')
        parser.add_argument('--date',  default=None,        help='Date YYYY-MM-DD')
        parser.add_argument('--force', action='store_true', help='Régénérer si déjà existant')
        args = parser.parse_args()
        self.generate(report_date=args.date, force=args.force)


if __name__ == '__main__':
    ReportGenerator().run()
