"""
generate_daily_report.py — Bulletin quotidien de VL CGF BRVM30 ETF
Usage : python generate_daily_report.py [--date YYYY-MM-DD] [--force]
"""

import os, sys, re, warnings
warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    Image, HRFlowable, PageBreak, KeepTogether,
)

from base import BaseScript

JOURS_FR = ['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche']
MOIS_FR  = ['janvier','février','mars','avril','mai','juin',
            'juillet','août','septembre','octobre','novembre','décembre']

def _date_fr(s):
    t = pd.Timestamp(s)
    return f"{JOURS_FR[t.weekday()]} {t.day} {MOIS_FR[t.month-1]} {t.year}"

def _dfr_short(s):
    t = pd.Timestamp(s)
    return f"{t.day:02d}/{t.month:02d}/{t.year}"


NAVY  = colors.HexColor("#1a3557")
GOLD  = colors.HexColor("#b8922f")
GRAY  = colors.HexColor("#6b7280")
LGRAY = colors.HexColor("#f3f4f6")
RULE  = colors.HexColor("#e5e7eb")
GREEN = colors.HexColor("#166534")
RED   = colors.HexColor("#991b1b")
BLACK = colors.HexColor("#111827")
WHITE = colors.white


class ReportGenerator(BaseScript):

    def __init__(self):
        super().__init__()
        self.PAGE_W, self.PAGE_H = A4
        self.M = 1.8 * cm
        self.PDFS_DIR = os.path.join(self.data_dir, 'pdfs')
        self.LOGO = os.path.join(self.root_dir, '1780762763961.jpg')

    def _load(self, f):
        p = os.path.join(self.data_dir, f)
        if not os.path.exists(p): return None
        import json; return json.load(open(p, encoding='utf-8'))

    def _scrape_sika_variations(self):
        try:
            import requests
            from bs4 import BeautifulSoup
            r = requests.get('https://sikafinance.com/marches/aaz',
                headers={'User-Agent':'Mozilla/5.0','Accept-Language':'fr-FR'},
                verify=False, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            out = {}
            for a in soup.find_all('a', href=re.compile(r'/marches/cotation_[A-Z]', re.I)):
                m = re.search(r'cotation_([A-Z0-9]+)', a['href'], re.I)
                if not m: continue
                tk = m.group(1).upper()
                if any(x in tk for x in ('BRVM','SIKA','COMPO')): continue
                row = a.find_parent('tr')
                if not row: continue
                cells = row.find_all(['td','th'])
                def _p(c): return c.get_text(strip=True).replace('\xa0','').replace(' ','').replace(',','.').replace('%','')
                if len(cells) >= 8:
                    try: out[tk] = {'dernier': float(_p(cells[6])), 'variation': float(_p(cells[7]))}
                    except: pass
            return out
        except Exception as e:
            print(f"  Sika scraping: {e}"); return {}

    # ── Styles ───────────────────────────────────────────────────────
    def _S(self):
        return {
            # header compact
            'h_fund':  ParagraphStyle('hf', fontName='Helvetica-Bold', fontSize=11,
                       textColor=WHITE, alignment=TA_RIGHT, leading=14),
            'h_sub':   ParagraphStyle('hs', fontName='Helvetica', fontSize=7.5,
                       textColor=colors.HexColor('#94a3b8'), alignment=TA_RIGHT, leading=10),
            # section titles
            'sec':     ParagraphStyle('sec', fontName='Helvetica-Bold', fontSize=7.5,
                       textColor=NAVY, leading=10, spaceBefore=0),
            # kpi — label au-dessus, valeur en-dessous
            'kl':      ParagraphStyle('kl', fontName='Helvetica', fontSize=6.5,
                       textColor=GRAY, leading=9, alignment=TA_LEFT),
            'kv':      ParagraphStyle('kv', fontName='Helvetica-Bold', fontSize=18,
                       textColor=NAVY, leading=22, alignment=TA_LEFT),
            'kv_pos':  ParagraphStyle('kvp', fontName='Helvetica-Bold', fontSize=18,
                       textColor=GREEN, leading=22, alignment=TA_LEFT),
            'kv_neg':  ParagraphStyle('kvn', fontName='Helvetica-Bold', fontSize=18,
                       textColor=RED, leading=22, alignment=TA_LEFT),
            'kv_sm':   ParagraphStyle('kvs', fontName='Helvetica-Bold', fontSize=13,
                       textColor=NAVY, leading=17, alignment=TA_LEFT),
            'kv_spos': ParagraphStyle('kvsp', fontName='Helvetica-Bold', fontSize=13,
                       textColor=GREEN, leading=17, alignment=TA_LEFT),
            'kv_sneg': ParagraphStyle('kvsn', fontName='Helvetica-Bold', fontSize=13,
                       textColor=RED, leading=17, alignment=TA_LEFT),
            'kl_sm':   ParagraphStyle('kls', fontName='Helvetica', fontSize=6,
                       textColor=GRAY, leading=8, alignment=TA_LEFT),
            # corps
            'body':    ParagraphStyle('b', fontName='Helvetica', fontSize=8,
                       textColor=BLACK, leading=11),
            'body_b':  ParagraphStyle('bb', fontName='Helvetica-Bold', fontSize=8,
                       textColor=NAVY, leading=11),
            'note':    ParagraphStyle('n', fontName='Helvetica-Oblique', fontSize=6.5,
                       textColor=GRAY, leading=8.5),
            # table
            'th':      ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=7.5,
                       textColor=WHITE, leading=10, alignment=TA_CENTER),
            'td':      ParagraphStyle('td', fontName='Helvetica', fontSize=7.5,
                       textColor=BLACK, leading=10, alignment=TA_CENTER),
            'td_pos':  ParagraphStyle('tdp', fontName='Helvetica-Bold', fontSize=7.5,
                       textColor=GREEN, leading=10, alignment=TA_CENTER),
            'td_neg':  ParagraphStyle('tdn', fontName='Helvetica-Bold', fontSize=7.5,
                       textColor=RED, leading=10, alignment=TA_CENTER),
        }

    def _sec_title(self, text, cw):
        S = self._S()
        return [
            Paragraph(text.upper(), S['sec']),
            HRFlowable(width=cw, thickness=0.8, color=GOLD, spaceBefore=2, spaceAfter=6),
        ]

    def _rule(self, cw):
        return HRFlowable(width=cw, thickness=0.4, color=RULE, spaceBefore=8, spaceAfter=8)

    def _kpi(self, label, value, style='kv'):
        S = self._S()
        return [Paragraph(label, S['kl']), Paragraph(str(value), S[style])]

    def _kpi_sm(self, label, value, style='kv_sm'):
        S = self._S()
        return [Paragraph(label, S['kl_sm']), Paragraph(str(value), S[style])]

    def _vs(self, v, big=True):
        if v is None: return 'kv' if big else 'kv_sm'
        if big: return 'kv_pos' if v>0 else ('kv_neg' if v<0 else 'kv')
        return 'kv_spos' if v>0 else ('kv_sneg' if v<0 else 'kv_sm')

    @staticmethod
    def _pct(v, dec=2, sign=True):
        if v is None: return '—'
        return f'{"+" if sign and v>0 else ""}{v:.{dec}f}%'

    # ── Graphiques ───────────────────────────────────────────────────
    def _chart_intraday(self, snaps, par, cw_pt):
        w_in = cw_pt / 72
        fig, ax = plt.subplots(figsize=(w_in, 2.6))
        fig.patch.set_facecolor('white'); ax.set_facecolor('white')
        times  = [s['time'] for s in snaps]
        vl_pts = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0) for s in snaps]
        xs = list(range(len(times)))
        ax.plot(xs, vl_pts, color='#1a3557', lw=1.8, zorder=3)
        ax.fill_between(xs, vl_pts, min(vl_pts)*0.9994, alpha=0.06, color='#1a3557')
        ax.axhline(y=par, color='#b8922f', ls='--', lw=1.0, alpha=0.7)
        vm, vM = min(vl_pts), max(vl_pts)
        ax.annotate(f'{vm:,.0f}', xy=(vl_pts.index(vm), vm), xytext=(0,-12),
                    textcoords='offset points', fontsize=7, color='#991b1b', ha='center')
        ax.annotate(f'{vM:,.0f}', xy=(vl_pts.index(vM), vM), xytext=(0,5),
                    textcoords='offset points', fontsize=7, color='#166534', ha='center')
        step = max(1, len(times)//8)
        ax.set_xticks(xs[::step]); ax.set_xticklabels(times[::step], rotation=35, ha='right', fontsize=7)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f'{x:,.0f}'))
        ax.tick_params(colors='#9ca3af', labelsize=7)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.spines['bottom'].set_visible(True); ax.spines['bottom'].set_color('#e5e7eb'); ax.spines['bottom'].set_linewidth(0.5)
        ax.grid(axis='y', color='#f3f4f6', lw=0.5, zorder=0)
        plt.tight_layout(pad=0.2)
        buf = BytesIO(); plt.savefig(buf, format='png', dpi=160, bbox_inches='tight'); plt.close(); buf.seek(0); return buf

    def _chart_base100(self, ih, launch_date, brvm_hist, brvm_at_launch, par, cw_pt):
        w_in = cw_pt / 72
        fig, ax = plt.subplots(figsize=(w_in, 2.4))
        fig.patch.set_facecolor('white'); ax.set_facecolor('white')
        lt = pd.Timestamp(launch_date)
        ep, bp = {}, {}
        for d, pts in sorted(ih.items()):
            if not pts or pd.Timestamp(d) < lt: continue
            lp = pts[-1]
            vl = lp.get('vl_fcfa') or lp.get('vl')
            bv = lp.get('brvm30_official') or brvm_hist.get(d)
            if vl: ep[pd.Timestamp(d)] = float(vl)/par*100
            if bv and brvm_at_launch: bp[pd.Timestamp(d)] = float(bv)/brvm_at_launch*100
        ep[lt] = 100.0
        if brvm_at_launch: bp[lt] = 100.0
        es = pd.Series(ep).sort_index(); bs = pd.Series(bp).sort_index()
        xs = list(range(len(es)))
        ax.plot(xs, es.values, color='#1a3557', lw=2.0, marker='o', ms=5, label='CGF BRVM30 ETF')
        if not bs.empty:
            bv2 = bs.reindex(es.index).values
            ax.plot(xs, bv2, color='#b8922f', lw=1.6, ls='--', marker='s', ms=3.5, label='BRVM30')
        ax.axhline(y=100, color='#d1d5db', ls=':', lw=0.7)
        labels = [d.strftime('%d/%m') for d in es.index]
        ax.set_xticks(xs); ax.set_xticklabels(labels, fontsize=9)
        ax.set_ylabel('Base 100', fontsize=7, color='#9ca3af')
        ax.tick_params(colors='#9ca3af', labelsize=7)
        ax.legend(fontsize=8, framealpha=0, loc='upper left')
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.spines['bottom'].set_visible(True); ax.spines['bottom'].set_color('#e5e7eb'); ax.spines['bottom'].set_linewidth(0.5)
        ax.grid(axis='y', color='#f3f4f6', lw=0.5)
        plt.tight_layout(pad=0.2)
        buf = BytesIO(); plt.savefig(buf, format='png', dpi=160, bbox_inches='tight'); plt.close(); buf.seek(0); return buf

    # ── Header compact (fond navy, logo + nom + date) ────────────────
    def _page_header(self, cw, etf_name, date_str, jours):
        logo = Image(self.LOGO, width=3.6*cm, height=1.0*cm, kind='proportional') \
               if os.path.exists(self.LOGO) else Paragraph('CGF', self._S()['h_fund'])
        S = self._S()
        right_col = [
            Paragraph(etf_name, S['h_fund']),
            Spacer(1, 2),
            Paragraph(f'Bulletin de VL · {date_str} · J+{jours} depuis le lancement', S['h_sub']),
        ]
        hdr = Table([[logo, right_col]], colWidths=[4*cm, cw-4*cm])
        hdr.setStyle(TableStyle([
            ('BACKGROUND', (0,0),(-1,-1), NAVY),
            ('VALIGN',     (0,0),(-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0),(-1,-1), 8),
            ('BOTTOMPADDING', (0,0),(-1,-1), 8),
            ('LEFTPADDING',   (0,0),(0,0), 12),
            ('RIGHTPADDING',  (1,0),(1,0), 12),
        ]))
        return [hdr, HRFlowable(width=cw, thickness=2.5, color=GOLD, spaceAfter=10)]

    # ── Corps principal ───────────────────────────────────────────────
    def generate(self, report_date=None, force=False):
        os.makedirs(self.PDFS_DIR, exist_ok=True)
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')
        pdf_path = os.path.join(self.PDFS_DIR, f'rapport_journalier_{report_date}.pdf')
        if os.path.exists(pdf_path) and not force:
            print(f"Rapport déjà existant : {pdf_path}"); return pdf_path

        print("Chargement des données...")
        nl     = self._load('nav_latest.json')          or {}
        intra  = self._load('intraday_nav.json')         or {}
        ih     = self._load('nav_intraday_history.json') or {}
        launch = self._load('launch_state.json')         or {}

        par         = float(launch.get('par_fcfa', 100000))
        launch_date = launch.get('launch_date', report_date)
        n_parts     = int(launch.get('n_parts', 0))
        etf_name    = nl.get('etf_name', 'CGF BRVM30 ETF')

        today_snaps = ih.get(report_date) or intra.get('snapshots', [])
        last_snap   = today_snaps[-1] if today_snaps else {}

        vl      = float(last_snap.get('vl_live_fcfa') or last_snap.get('vl_fcfa') or last_snap.get('vl') or par)
        var_j   = last_snap.get('change_1d_pct') or last_snap.get('change_day_pct')
        aum     = float(last_snap.get('aum_mfcfa') or nl.get('aum_mfcfa') or 0)
        perf_l  = last_snap.get('perf_since_launch')
        n_prix  = int(last_snap.get('n_prices') or nl.get('n_live_prices') or 0)
        heure   = last_snap.get('time','—')

        brvm_h   = self._load('brvm30_index_history.json') or {}
        brvm_al  = float(launch.get('brvm30_index_at_launch') or brvm_h.get(launch_date) or 0) or None
        brvm_now = last_snap.get('brvm30_official')
        if not brvm_now and brvm_h:
            brvm_now = brvm_h.get(report_date) or float(brvm_h[max(brvm_h.keys())])
        perf_idx = (float(brvm_now)/brvm_al - 1)*100 if brvm_now and brvm_al else None

        lt = pd.Timestamp(launch_date)
        ce, ci = {}, {}
        for d, pts in ih.items():
            if pts and pd.Timestamp(d) >= lt:
                lp = pts[-1]
                v2 = lp.get('vl_fcfa') or lp.get('vl')
                bv = lp.get('brvm30_official')
                if v2: ce[d] = float(v2)
                if bv: ci[d] = float(bv)
                elif d in brvm_h: ci[d] = float(brvm_h[d])

        te = td = None
        ec = pd.Series(ce).sort_index(); ic = pd.Series(ci).sort_index()
        ns = len(ec)
        if len(ec) >= 2 and len(ic) >= 2:
            re_ = ec.pct_change().dropna(); ri_ = ic.pct_change().dropna()
            cm_ = re_.index.intersection(ri_.index)
            if len(cm_) >= 1:
                act = re_.loc[cm_] - ri_.loc[cm_]
                te  = float(act.std()*np.sqrt(252)*100) if len(cm_)>=2 else float(abs(act.iloc[0])*np.sqrt(252)*100)
            if brvm_al and not ec.empty and not ic.empty:
                td = (ec.iloc[-1]/par / (ic.iloc[-1]/brvm_al) - 1)*100

        cagr = sharpe = maxdd = None
        if len(ec) >= 2:
            rl = ec.pct_change().dropna()
            cagr = ((1 + ec.iloc[-1]/ec.iloc[0] - 1)**(252/len(ec)) - 1)*100
            if rl.std() > 0: sharpe = float((rl.mean()-0.035/252)/rl.std()*(252**0.5))
            maxdd = float(((ec - ec.cummax())/ec.cummax()*100).min())
        elif len(ec) == 1: maxdd = 0.0

        last_r = nl.get('last_rebal_date')
        next_r_str = '—'; days_r = None; prog_r = None
        if last_r:
            try:
                from dateutil.relativedelta import relativedelta
                lr = pd.Timestamp(last_r)
                nr = lr + relativedelta(months=3)
                while nr.weekday() >= 5: nr += pd.Timedelta(days=1)
                next_r_str = _dfr_short(nr)
                days_r = (nr - pd.Timestamp(report_date).normalize()).days
                total_d = (nr - lr).days
                prog_r = max(0, min(100, int((1-days_r/total_d)*100))) if total_d > 0 else None
            except: pass

        jours_lct = (pd.Timestamp(report_date) - pd.Timestamp(launch_date)).days
        non_repr  = ns < 30

        print("Scraping Sika..."); sika = self._scrape_sika_variations()
        basket = nl.get('basket', [])

        print("Génération PDF...")
        S  = self._S()
        cw = self.PAGE_W - 2*self.M

        doc = SimpleDocTemplate(pdf_path, pagesize=A4,
            leftMargin=self.M, rightMargin=self.M,
            topMargin=0.7*cm, bottomMargin=0.8*cm,
            title=f'Bulletin VL — {etf_name} — {report_date}',
            author='CGF Gestion')

        story = []
        date_fr = _date_fr(report_date)

        # ── En-tête ──────────────────────────────────────────────────
        story += self._page_header(cw, etf_name, date_fr, jours_lct+1)

        # ── Bloc identification (une ligne, texte simple) ─────────────
        id_line = (
            f'<b>Gestionnaire :</b> CGF Gestion — Agréé CREPMF  ·  '
            f'<b>Type :</b> OPCVM indiciel coté (ETF) — Distribuant  ·  '
            f'<b>Marché :</b> BRVM  ·  '
            f'<b>Indice de référence :</b> BRVM30 (indice de cours — dividendes redistribués aux porteurs)  ·  '
            f'<b>Lancement :</b> {_dfr_short(launch_date)}  ·  '
            f'<b>VE :</b> {par:,.0f} FCFA / part'
        )
        story.append(Paragraph(id_line, ParagraphStyle('id', fontName='Helvetica', fontSize=7,
                     textColor=GRAY, leading=10, spaceAfter=0)))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceBefore=5, spaceAfter=10))

        # ══════════════════════════════════════════════════════════════
        # VALEUR LIQUIDATIVE — grandes métriques sur fond blanc
        # ══════════════════════════════════════════════════════════════
        story += self._sec_title('Valeur liquidative officielle de clôture', cw)

        kpi1 = [
            self._kpi('VL PAR PART',   f'{vl:,.0f} FCFA', self._vs(None)),
            self._kpi('VARIATION J/J', self._pct(var_j),  self._vs(var_j)),
            self._kpi('ACTIF NET',     f'{aum:,.1f} M FCFA', self._vs(None)),
            self._kpi('PARTS',         f'{n_parts:,}',    self._vs(None)),
            self._kpi('COURS BRVM',    f'{n_prix}/27',    self._vs(None) if n_prix>=25 else 'kv_neg'),
        ]
        t1 = Table([kpi1], colWidths=[cw/5]*5)
        t1.setStyle(TableStyle([
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING',    (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 8),
            ('LEFTPADDING',   (0,0),(-1,-1), 0),
            ('RIGHTPADDING',  (0,0),(-1,-1), 0),
            ('LINEBELOW',     (0,-1),(-1,-1), 0.4, RULE),
        ]))
        story.append(t1)
        story.append(Spacer(1, 10))

        # ══════════════════════════════════════════════════════════════
        # PERFORMANCE + RISQUE sur deux colonnes
        # ══════════════════════════════════════════════════════════════
        story += self._sec_title(f'Performance depuis le lancement  ({_dfr_short(launch_date)})', cw)

        td_str = f'{td:+.3f}%  ({int(round(td*100))} bps)' if td is not None else '—'
        perf_kpis = [
            self._kpi('ETF CGF BRVM30',         self._pct(perf_l),  self._vs(perf_l)),
            self._kpi('BRVM30 INDICE DE COURS',  self._pct(perf_idx),self._vs(perf_idx)),
            self._kpi('TRACKING DIFFERENCE (TD)', td_str,            self._vs(td)),
            self._kpi('BRVM30 NIVEAU',           f'{float(brvm_now):.2f}' if brvm_now else '—', self._vs(None)),
            self._kpi('DERNIER iNAV',            heure+' UTC',      self._vs(None)),
        ]
        t2 = Table([perf_kpis], colWidths=[cw/5]*5)
        t2.setStyle(TableStyle([
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING',    (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 8),
            ('LEFTPADDING',   (0,0),(-1,-1), 0),
            ('RIGHTPADDING',  (0,0),(-1,-1), 0),
            ('LINEBELOW',     (0,-1),(-1,-1), 0.4, RULE),
        ]))
        story.append(t2)
        story.append(Paragraph(
            'Fonds distribuant : dividendes collectés versés aux porteurs. '
            'La TD est structurellement positive en période de dividendes (mars–sept.) '
            'car l\'ETF perçoit les coupons que l\'indice de cours BRVM30 n\'intègre pas.',
            S['note']))
        story.append(Spacer(1, 10))

        # ── Indicateurs de risque ─────────────────────────────────────
        star = '  ⁽*⁾' if non_repr else ''
        story += self._sec_title(
            'Indicateurs de réplication et de risque' + ('  —  données préliminaires < 30 séances' if non_repr else ''), cw)

        risk_kpis = [
            self._kpi_sm('TRACKING ERROR',       f'{te:.2f}%' if te else '—',             self._vs(None, False)),
            self._kpi_sm('RATIO DE SHARPE'+star, f'{sharpe:.2f}' if sharpe else '—',      self._vs(None, False)),
            self._kpi_sm('CAGR ANNUALISÉ'+star,  self._pct(cagr) if cagr else '—',        self._vs(cagr, False)),
            self._kpi_sm('MAX DRAWDOWN',          self._pct(maxdd) if maxdd is not None else '—', self._vs(maxdd, False)),
            self._kpi_sm('SÉANCES',               str(ns),                                 self._vs(None, False)),
            self._kpi_sm('RÉFÉRENCE NAV',         _dfr_short(nl.get('calc_date',report_date)), self._vs(None, False)),
        ]
        t3 = Table([risk_kpis], colWidths=[cw/6]*6)
        t3.setStyle(TableStyle([
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING',    (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 8),
            ('LEFTPADDING',   (0,0),(-1,-1), 0),
            ('RIGHTPADDING',  (0,0),(-1,-1), 0),
            ('LINEBELOW',     (0,-1),(-1,-1), 0.4, RULE),
        ]))
        story.append(t3)
        if non_repr:
            story.append(Paragraph(
                '⁽*⁾ TE = σ(rendements actifs) × √252  ·  Sharpe = (r̄ − rf) / σ × √252, rf = 3,5% BCEAO  ·  '
                'TD = (perf ETF / perf BRVM30) − 1', S['note']))
        story.append(Spacer(1, 10))

        # ── iNAV intraday ─────────────────────────────────────────────
        story += self._sec_title(
            f'iNAV intraday — {len(today_snaps)} valorisations' if today_snaps else 'iNAV intraday', cw)
        if today_snaps:
            vls = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0) for s in today_snaps]
            story.append(Paragraph(
                f'Min {min(vls):,.0f} · Max {max(vls):,.0f} · Clôture {vl:,.0f} FCFA  ·  '
                f'Prix d\'émission {par:,.0f} FCFA (pointillés)',
                S['note']))
            story.append(Spacer(1, 3))
            buf1 = self._chart_intraday(today_snaps, par, cw)
            story.append(Image(buf1, width=cw, height=cw*2.6/7.22))
        else:
            story.append(Paragraph('Aucune donnée iNAV disponible pour cette séance.', S['note']))
        story.append(Spacer(1, 10))

        # ── Rebalancement ─────────────────────────────────────────────
        reb_items = [
            self._kpi_sm('DERNIER REBALANCEMENT',   _dfr_short(last_r) if last_r else '—',    self._vs(None, False)),
            self._kpi_sm('PROCHAIN (ESTIMÉ)',        next_r_str,                                self._vs(None, False)),
            self._kpi_sm('JOURS RESTANTS',           f'{days_r}j' if days_r is not None else '—',
                         'kv_sneg' if days_r is not None and days_r<=10 else 'kv_sm'),
            self._kpi_sm('AVANCEMENT CYCLE',         f'{prog_r}%' if prog_r is not None else '—', self._vs(None, False)),
            self._kpi_sm('JOURS DEPUIS LANCEMENT',   f'{jours_lct+1}j',                         self._vs(None, False)),
        ]
        reb_tbl = Table([reb_items], colWidths=[cw/5]*5)
        reb_tbl.setStyle(TableStyle([
            ('VALIGN',        (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING',    (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 8),
            ('LEFTPADDING',   (0,0),(-1,-1), 0),
            ('RIGHTPADDING',  (0,0),(-1,-1), 0),
            ('LINEABOVE',     (0,0),(-1,0),  0.4, RULE),
            ('LINEBELOW',     (0,-1),(-1,-1), 0.4, RULE),
        ]))
        reb_block = self._sec_title('Rebalancement BRVM30', cw) + [reb_tbl]
        story.append(KeepTogether(reb_block))

        story.append(Spacer(1, 8))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=4))
        story.append(Paragraph(
            f'Document réglementaire · CGF Gestion — Agréé CREPMF · '
            f'Généré le {datetime.now().strftime("%d/%m/%Y %H:%M")} UTC · '
            f'Source : sikafinance.com · Les performances passées ne préjugent pas des performances futures.',
            S['note']))

        # ══════════════════════════════════════════════════════════════
        # PAGE 2 — PORTEFEUILLE
        # ══════════════════════════════════════════════════════════════
        story.append(PageBreak())
        story += self._page_header(cw, etf_name, date_fr, jours_lct+1)

        # ── Base 100 ─────────────────────────────────────────────────
        nd = sum(1 for d,pts in ih.items() if pts and pd.Timestamp(d) >= lt)
        story += self._sec_title(
            f'Performance relative depuis le lancement — base 100 au {_dfr_short(launch_date)}  ·  {nd} séance(s)', cw)
        if nd >= 1:
            buf2 = self._chart_base100(ih, launch_date, brvm_h, brvm_al, par, cw)
            story.append(Image(buf2, width=cw, height=cw*1.55/7.22))
        else:
            story.append(Paragraph('Données insuffisantes.', S['note']))
        story.append(Spacer(1, 10))

        # ── Tableau portefeuille ──────────────────────────────────────
        story += self._sec_title(
            f'Composition du portefeuille — {len(basket)} titres  ·  AUM {aum:,.1f} M FCFA  ·  NAV au {nl.get("calc_date","—")}', cw)

        cws = [cw*x for x in [0.10, 0.08, 0.12, 0.095, 0.12, 0.09, 0.395]]
        hrow = [Paragraph(h, S['th']) for h in ['Ticker','Poids','Clôture J-1','Var. J','Cours live','Qté','Valeur (M FCFA)']]
        rows = [hrow]; vars_ = []
        for r in basket:
            tk = r['ticker'].upper()
            sk = sika.get(tk, {})
            vj = sk.get('variation') if isinstance(sk,dict) else None
            co = sk.get('dernier')   if isinstance(sk,dict) else None
            qt = r.get('quantite') or r.get('qty') or '—'
            vars_.append(vj)
            rows.append([
                Paragraph(tk,  S['td']),
                Paragraph(f"{r['poids_pct']:.2f}%", S['td']),
                Paragraph(f"{int(r['dernier_prix']):,}" if r.get('dernier_prix') else '—', S['td']),
                Paragraph(f'{vj:+.2f}%' if vj is not None else '—',
                          S['td_pos'] if (vj or 0)>0 else (S['td_neg'] if (vj or 0)<0 else S['td'])),
                Paragraph(f"{int(co):,}" if co else '—', S['td']),
                Paragraph(f"{int(qt):,}" if isinstance(qt,(int,float)) else str(qt), S['td']),
                Paragraph(f"{r['pv_mfcfa']:.2f}", S['td']),
            ])

        pt = Table(rows, colWidths=cws, repeatRows=1)
        sc = [
            ('BACKGROUND',    (0,0),(-1,0),  NAVY),
            ('LINEBELOW',     (0,0),(-1,0),  1.0, GOLD),
            ('FONTSIZE',      (0,0),(-1,-1), 7),
            ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
            ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
            ('TOPPADDING',    (0,0),(-1,-1), 2),
            ('BOTTOMPADDING', (0,0),(-1,-1), 2),
            ('LINEBELOW',     (0,-1),(-1,-1), 0.5, RULE),
            ('LINEABOVE',     (0,1),(-1,-1),  0.2, RULE),
        ]
        for i in range(1, len(rows)):
            if i % 2 == 0:
                sc.append(('BACKGROUND', (0,i),(-1,i), LGRAY))
        pt.setStyle(TableStyle(sc))
        story.append(pt)
        story.append(Spacer(1, 10))

        # ── Top mouvements ────────────────────────────────────────────
        bv_ = [(r['ticker'].upper(), r['poids_pct'], sika.get(r['ticker'].upper(),{}).get('variation'))
               for r in basket
               if isinstance(sika.get(r['ticker'].upper(),{}),dict)
               and sika.get(r['ticker'].upper(),{}).get('variation') is not None]
        if bv_:
            top5 = sorted(bv_, key=lambda x:x[2], reverse=True)[:5]
            bot5 = sorted(bv_, key=lambda x:x[2])[:5]
            gap   = 12
            hw    = (cw - gap) / 2
            mk_hdr = lambda txt: Paragraph(txt, ParagraphStyle(
                'mh', fontName='Helvetica-Bold', fontSize=7.5,
                textColor=WHITE, alignment=TA_CENTER, leading=10))
            def mk_rows(data, pos_style):
                rows = [[mk_hdr('Ticker'), mk_hdr('Poids'), mk_hdr('Variation')]]
                for tk, pds, vj in data:
                    rows.append([Paragraph(tk, S['td']),
                                  Paragraph(f'{pds:.2f}%', S['td']),
                                  Paragraph(f'{vj:+.2f}%', pos_style)])
                return rows
            t_top = Table(mk_rows(top5, S['td_pos']), colWidths=[hw*0.34, hw*0.33, hw*0.33])
            t_bot = Table(mk_rows(bot5, S['td_neg']), colWidths=[hw*0.34, hw*0.33, hw*0.33])
            for t, bg in [(t_top, GREEN), (t_bot, RED)]:
                t.setStyle(TableStyle([
                    ('BACKGROUND',    (0,0),(-1,0), bg),
                    ('LINEBELOW',     (0,0),(-1,0), 0.5, WHITE),
                    ('FONTSIZE',      (0,0),(-1,-1), 7.5),
                    ('ALIGN',         (0,0),(-1,-1), 'CENTER'),
                    ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
                    ('TOPPADDING',    (0,0),(-1,-1), 3),
                    ('BOTTOMPADDING', (0,0),(-1,-1), 3),
                    ('LINEABOVE',     (0,1),(-1,-1), 0.2, RULE),
                ]))
            lbl_style = ParagraphStyle('ml', fontName='Helvetica-Bold', fontSize=7,
                                       leading=9, spaceAfter=3)
            lbl_h = Paragraph('TOP 5 HAUSSES', ParagraphStyle('lh', fontName='Helvetica-Bold',
                              fontSize=7, textColor=GREEN, leading=9, spaceAfter=3))
            lbl_b = Paragraph('TOP 5 BAISSES', ParagraphStyle('lb', fontName='Helvetica-Bold',
                              fontSize=7, textColor=RED, leading=9, spaceAfter=3))
            movers = Table([[lbl_h, lbl_b], [t_top, t_bot]],
                           colWidths=[hw, hw])
            movers.setStyle(TableStyle([
                ('VALIGN',        (0,0),(-1,-1), 'TOP'),
                ('TOPPADDING',    (0,0),(-1,-1), 0),
                ('BOTTOMPADDING', (0,0),(-1,-1), 0),
                ('LEFTPADDING',   (0,0),(-1,-1), 0),
                ('RIGHTPADDING',  (0,0),(0,-1),  gap),
                ('RIGHTPADDING',  (1,0),(1,-1),  0),
            ]))
            story.append(KeepTogether(self._sec_title('Top mouvements du jour', cw) + [movers]))

        story.append(Spacer(1, 8))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=4))
        story.append(Paragraph(
            f'Document réglementaire à usage interne · CGF Gestion — Agréé CREPMF · '
            f'Généré le {datetime.now().strftime("%d/%m/%Y %H:%M")} UTC · '
            f'Source : sikafinance.com · Ce document ne constitue pas un conseil en investissement.',
            S['note']))

        doc.build(story)
        print(f'Rapport généré : {pdf_path}')
        return pdf_path

    def run(self):
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument('--date', default=None)
        p.add_argument('--force', action='store_true')
        a = p.parse_args()
        self.generate(report_date=a.date, force=a.force)

if __name__ == '__main__':
    ReportGenerator().run()
