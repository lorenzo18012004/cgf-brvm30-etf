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

JOURS_FR = ['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche']
MOIS_FR  = ['janvier','février','mars','avril','mai','juin',
            'juillet','août','septembre','octobre','novembre','décembre']

def _date_fr(s):
    t = pd.Timestamp(s)
    return f"{JOURS_FR[t.weekday()]} {t.day} {MOIS_FR[t.month-1]} {t.year}"

def _dfr(s):
    t = pd.Timestamp(s)
    return f"{t.day:02d}/{t.month:02d}/{t.year}"

NAVY  = colors.HexColor("#1a3557")
GOLD  = colors.HexColor("#b8922f")
GRAY  = colors.HexColor("#9ca3af")
DGRAY = colors.HexColor("#6b7280")
RULE  = colors.HexColor("#e5e7eb")
GREEN = colors.HexColor("#166534")
RED   = colors.HexColor("#991b1b")
WHITE = colors.white
LGRAY = colors.HexColor("#f9fafb")


class ReportGenerator(BaseScript):

    def __init__(self):
        super().__init__()
        self.PAGE_W, self.PAGE_H = A4
        self.M = 2.0 * cm
        self.PDFS_DIR = os.path.join(self.data_dir, 'pdfs')
        self.LOGO = os.path.join(self.root_dir, '1780762763961.jpg')

    def _load(self, f):
        p = os.path.join(self.data_dir, f)
        if not os.path.exists(p): return None
        import json; return json.load(open(p, encoding='utf-8'))

    def _scrape_sika(self):
        try:
            import requests
            from bs4 import BeautifulSoup
            r = requests.get('https://sikafinance.com/marches/aaz',
                headers={'User-Agent':'Mozilla/5.0'}, verify=False, timeout=15)
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
        except: return {}

    # ── styles ───────────────────────────────────────────────────────
    def S(self):
        return {
            # en-tête page
            'h_name': ParagraphStyle('hn', fontName='Helvetica-Bold', fontSize=11,
                      textColor=WHITE, alignment=TA_RIGHT, leading=14),
            'h_sub':  ParagraphStyle('hs', fontName='Helvetica', fontSize=7.5,
                      textColor=colors.HexColor('#94a3b8'), alignment=TA_RIGHT, leading=10),
            # micro label (au-dessus des grands chiffres)
            'lbl':    ParagraphStyle('lbl', fontName='Helvetica', fontSize=6.5,
                      textColor=GRAY, leading=9, spaceBefore=0, spaceAfter=0),
            'lbl_r':  ParagraphStyle('lblr', fontName='Helvetica', fontSize=6.5,
                      textColor=GRAY, leading=9, alignment=TA_RIGHT),
            # grands chiffres
            'giant':  ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=40,
                      textColor=NAVY, leading=46),
            'big':    ParagraphStyle('b', fontName='Helvetica-Bold', fontSize=26,
                      textColor=NAVY, leading=30),
            'big_pos':ParagraphStyle('bp', fontName='Helvetica-Bold', fontSize=26,
                      textColor=GREEN, leading=30),
            'big_neg':ParagraphStyle('bn', fontName='Helvetica-Bold', fontSize=26,
                      textColor=RED, leading=30),
            'med':    ParagraphStyle('m', fontName='Helvetica-Bold', fontSize=18,
                      textColor=NAVY, leading=22),
            'med_pos':ParagraphStyle('mp', fontName='Helvetica-Bold', fontSize=18,
                      textColor=GREEN, leading=22),
            'med_neg':ParagraphStyle('mn', fontName='Helvetica-Bold', fontSize=18,
                      textColor=RED, leading=22),
            # résumé bas de page 1
            'summary':ParagraphStyle('s', fontName='Helvetica', fontSize=7.5,
                      textColor=DGRAY, leading=11),
            # note légale
            'note':   ParagraphStyle('n', fontName='Helvetica-Oblique', fontSize=6.5,
                      textColor=GRAY, leading=8.5),
            # tableau
            'th':     ParagraphStyle('th', fontName='Helvetica-Bold', fontSize=7.5,
                      textColor=WHITE, alignment=TA_CENTER, leading=10),
            'td':     ParagraphStyle('td', fontName='Helvetica', fontSize=7.5,
                      textColor=colors.HexColor('#111827'), alignment=TA_CENTER, leading=10),
            'tdg':    ParagraphStyle('tdg', fontName='Helvetica-Bold', fontSize=7.5,
                      textColor=GREEN, alignment=TA_CENTER, leading=10),
            'tdr':    ParagraphStyle('tdr', fontName='Helvetica-Bold', fontSize=7.5,
                      textColor=RED, alignment=TA_CENTER, leading=10),
        }

    @staticmethod
    def _pct(v, dec=2):
        if v is None: return '—'
        return f'{"+" if v>0 else ""}{v:.{dec}f}%'

    def _vs(self, v, size='big'):
        s = self.S()
        if size == 'big':
            if v is None: return s['big']
            return s['big_pos'] if v>0 else (s['big_neg'] if v<0 else s['big'])
        if v is None: return s['med']
        return s['med_pos'] if v>0 else (s['med_neg'] if v<0 else s['med'])

    # ── graphiques ───────────────────────────────────────────────────
    def _chart_intraday(self, snaps, par, cw_pt):
        fig, ax = plt.subplots(figsize=(cw_pt/72, 2.8))
        fig.patch.set_facecolor('white'); ax.set_facecolor('white')
        times  = [s['time'] for s in snaps]
        vls    = [float(s.get('vl_live_fcfa') or s.get('vl_fcfa') or s.get('vl') or 0) for s in snaps]
        xs = list(range(len(times)))
        ax.plot(xs, vls, color='#1a3557', lw=2.0, zorder=3)
        ax.fill_between(xs, vls, min(vls)*0.9994, alpha=0.05, color='#1a3557')
        ax.axhline(par, color='#b8922f', ls='--', lw=1.0, alpha=0.6)
        vm, vM = min(vls), max(vls)
        ax.annotate(f'{vm:,.0f}', xy=(vls.index(vm), vm), xytext=(0,-13),
                    textcoords='offset points', fontsize=7.5, color='#991b1b', ha='center')
        ax.annotate(f'{vM:,.0f}', xy=(vls.index(vM), vM), xytext=(0,6),
                    textcoords='offset points', fontsize=7.5, color='#166534', ha='center')
        step = max(1, len(times)//8)
        ax.set_xticks(xs[::step]); ax.set_xticklabels(times[::step], fontsize=8, ha='center')
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f'{x:,.0f}'))
        ax.tick_params(colors='#9ca3af', labelsize=8)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.spines['bottom'].set_visible(True)
        ax.spines['bottom'].set_color('#e5e7eb'); ax.spines['bottom'].set_linewidth(0.5)
        ax.grid(axis='y', color='#f3f4f6', lw=0.5)
        plt.tight_layout(pad=0.3)
        buf = BytesIO(); plt.savefig(buf, format='png', dpi=160, bbox_inches='tight'); plt.close(); buf.seek(0); return buf

    def _chart_base100(self, ih, launch_date, brvm_h, brvm_al, par, cw_pt):
        fig, ax = plt.subplots(figsize=(cw_pt/72, 2.4))
        fig.patch.set_facecolor('white'); ax.set_facecolor('white')
        lt = pd.Timestamp(launch_date)
        ep, bp = {lt: 100.0}, {}
        if brvm_al: bp[lt] = 100.0
        for d, pts in sorted(ih.items()):
            if not pts or pd.Timestamp(d) < lt: continue
            lp = pts[-1]
            vl = lp.get('vl_fcfa') or lp.get('vl')
            bv = lp.get('brvm30_official') or brvm_h.get(d)
            if vl: ep[pd.Timestamp(d)] = float(vl)/par*100
            if bv and brvm_al: bp[pd.Timestamp(d)] = float(bv)/brvm_al*100
        es = pd.Series(ep).sort_index(); bs = pd.Series(bp).sort_index()
        xs = list(range(len(es)))
        ax.plot(xs, es.values, color='#1a3557', lw=2.0, marker='o', ms=5, label='CGF BRVM30 ETF')
        if not bs.empty:
            ax.plot(xs, bs.reindex(es.index).values, color='#b8922f', lw=1.5,
                    ls='--', marker='s', ms=3.5, label='BRVM30')
        ax.axhline(100, color='#d1d5db', ls=':', lw=0.7)
        labels = [d.strftime('%d/%m') for d in es.index]
        ax.set_xticks(xs); ax.set_xticklabels(labels, fontsize=9)
        ax.set_ylabel('Base 100', fontsize=7.5, color='#9ca3af')
        ax.tick_params(colors='#9ca3af', labelsize=8)
        ax.legend(fontsize=9, framealpha=0, loc='upper left')
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.spines['bottom'].set_visible(True); ax.spines['bottom'].set_color('#e5e7eb')
        ax.grid(axis='y', color='#f3f4f6', lw=0.5)
        plt.tight_layout(pad=0.3)
        buf = BytesIO(); plt.savefig(buf, format='png', dpi=160, bbox_inches='tight'); plt.close(); buf.seek(0); return buf

    # ── header compact ───────────────────────────────────────────────
    def _header(self, cw, etf_name, date_str):
        logo = Image(self.LOGO, width=3.6*cm, height=1.0*cm, kind='proportional') \
               if os.path.exists(self.LOGO) else Paragraph('CGF', self.S()['h_name'])
        s = self.S()
        right = [Paragraph(etf_name, s['h_name']), Spacer(1,2),
                 Paragraph(f'Bulletin de valeur liquidative  ·  {date_str}', s['h_sub'])]
        hdr = Table([[logo, right]], colWidths=[4*cm, cw-4*cm])
        hdr.setStyle(TableStyle([
            ('BACKGROUND',    (0,0),(-1,-1), NAVY),
            ('VALIGN',        (0,0),(-1,-1), 'MIDDLE'),
            ('TOPPADDING',    (0,0),(-1,-1), 9),
            ('BOTTOMPADDING', (0,0),(-1,-1), 9),
            ('LEFTPADDING',   (0,0),(0,0),   12),
            ('RIGHTPADDING',  (1,0),(1,0),   12),
        ]))
        return [hdr, HRFlowable(width=cw, thickness=2.5, color=GOLD, spaceAfter=0)]

    # ── génération ───────────────────────────────────────────────────
    def generate(self, report_date=None, force=False):
        os.makedirs(self.PDFS_DIR, exist_ok=True)
        if report_date is None:
            report_date = datetime.now().strftime('%Y-%m-%d')
        pdf_path = os.path.join(self.PDFS_DIR, f'rapport_journalier_{report_date}.pdf')
        if os.path.exists(pdf_path) and not force:
            print(f"Existant : {pdf_path}"); return pdf_path

        print("Chargement...")
        nl     = self._load('nav_latest.json')          or {}
        intra  = self._load('intraday_nav.json')         or {}
        ih     = self._load('nav_intraday_history.json') or {}
        launch = self._load('launch_state.json')         or {}

        par         = float(launch.get('par_fcfa', 100000))
        launch_date = launch.get('launch_date', report_date)
        n_parts     = int(launch.get('n_parts', 0))
        etf_name    = nl.get('etf_name', 'CGF BRVM30 ETF')

        snaps     = ih.get(report_date) or intra.get('snapshots', [])
        last      = snaps[-1] if snaps else {}

        vl    = float(last.get('vl_live_fcfa') or last.get('vl_fcfa') or last.get('vl') or par)
        var_j = last.get('change_1d_pct') or last.get('change_day_pct')
        aum   = float(last.get('aum_mfcfa') or nl.get('aum_mfcfa') or 0)
        perf_l= last.get('perf_since_launch')
        heure = last.get('time','—')
        n_prix= int(last.get('n_prices') or nl.get('n_live_prices') or 0)

        brvm_h  = self._load('brvm30_index_history.json') or {}
        brvm_al = float(launch.get('brvm30_index_at_launch') or brvm_h.get(launch_date) or 0) or None
        brvm_now= last.get('brvm30_official')
        if not brvm_now and brvm_h:
            brvm_now = brvm_h.get(report_date) or float(brvm_h[max(brvm_h.keys())])
        perf_idx= (float(brvm_now)/brvm_al - 1)*100 if brvm_now and brvm_al else None

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

        last_r = nl.get('last_rebal_date')
        next_r_str = '—'; days_r = None
        if last_r:
            try:
                from dateutil.relativedelta import relativedelta
                lr = pd.Timestamp(last_r)
                nr = lr + relativedelta(months=3)
                while nr.weekday() >= 5: nr += pd.Timedelta(days=1)
                next_r_str = _dfr(nr)
                days_r = (nr - pd.Timestamp(report_date).normalize()).days
            except: pass

        jours_lct = (pd.Timestamp(report_date) - pd.Timestamp(launch_date)).days
        print("Scraping Sika..."); sika = self._scrape_sika()
        basket = nl.get('basket', [])

        print("PDF...")
        s   = self.S()
        cw  = self.PAGE_W - 2*self.M

        doc = SimpleDocTemplate(pdf_path, pagesize=A4,
            leftMargin=self.M, rightMargin=self.M,
            topMargin=0.8*cm, bottomMargin=0.8*cm,
            title=f'Bulletin VL — {etf_name} — {report_date}',
            author='CGF Gestion')

        story = []

        # ══ PAGE 1 ════════════════════════════════════════════════════

        story += self._header(cw, etf_name, _date_fr(report_date))
        story.append(Spacer(1, 32))

        # ── VL   +   VARIATION ───────────────────────────────────────
        vl_col = [
            Paragraph('VALEUR LIQUIDATIVE  ·  CLÔTURE OFFICIELLE', s['lbl']),
            Spacer(1, 10),
            Paragraph(f'{vl:,.0f}', s['giant']),
            Spacer(1, 4),
            Paragraph('FCFA PAR PART', s['lbl']),
        ]
        var_col = [
            Paragraph('VARIATION JOURNALIÈRE', s['lbl_r']),
            Spacer(1, 10),
            Paragraph(self._pct(var_j), self._vs(var_j, 'big')),
            Spacer(1, 4),
            Paragraph(f'iNAV jusqu\'à {heure} UTC  ·  {n_prix}/27 cours BRVM', s['lbl_r']),
        ]
        t_vl = Table([[vl_col, var_col]], colWidths=[cw*0.52, cw*0.48])
        t_vl.setStyle(TableStyle([
            ('VALIGN', (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING', (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 0),
            ('LEFTPADDING', (0,0),(-1,-1), 0),
            ('RIGHTPADDING', (0,0),(-1,-1), 0),
        ]))
        story.append(t_vl)
        story.append(Spacer(1, 30))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=24))

        # ── PERFORMANCE ──────────────────────────────────────────────
        td_bps = f'{int(round(td*100)):+d} bps' if td is not None else '—'
        perf_data = [
            [s['lbl'], self._vs(perf_l,'big')],
            [s['lbl'], self._vs(perf_idx,'big')],
            [s['lbl'], self._vs(td,'med')],
            [s['lbl'], s['med']],
        ]
        perf_labels = ['ETF CGF BRVM30', 'BRVM30', 'TRACKING DIFF.', 'ACTIF NET']
        perf_values = [self._pct(perf_l), self._pct(perf_idx), td_bps, f'{aum:,.1f} M FCFA']
        perf_src    = [f'depuis le {_dfr(launch_date)}', f'indice de cours', 'TD structurelle en div.', f'{n_parts:,} parts']

        perf_cells = []
        for lbl, val, src, (ls, vs) in zip(perf_labels, perf_values, perf_src, perf_data):
            perf_cells.append([
                Paragraph(lbl, ls),
                Spacer(1, 6),
                Paragraph(val, vs),
                Spacer(1, 4),
                Paragraph(src, ls),
            ])

        t_perf = Table([perf_cells], colWidths=[cw/4]*4)
        t_perf.setStyle(TableStyle([
            ('VALIGN', (0,0),(-1,-1), 'TOP'),
            ('TOPPADDING', (0,0),(-1,-1), 0),
            ('BOTTOMPADDING', (0,0),(-1,-1), 0),
            ('LEFTPADDING', (0,0),(-1,-1), 0),
            ('RIGHTPADDING', (0,0),(-1,-1), 0),
            ('LINEBEFORE', (1,0),(3,0), 0.4, RULE),
        ]))
        story.append(t_perf)
        story.append(Spacer(1, 30))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=20))

        # ── iNAV INTRADAY ─────────────────────────────────────────────
        story.append(Paragraph(
            f'iNAV INTRADAY  ·  {len(snaps)} valorisations  ·  min {min(float(x.get("vl_live_fcfa") or x.get("vl_fcfa") or x.get("vl") or 0) for x in snaps):,.0f}  –  max {max(float(x.get("vl_live_fcfa") or x.get("vl_fcfa") or x.get("vl") or 0) for x in snaps):,.0f} FCFA' if snaps else 'iNAV INTRADAY  ·  aucune donnée', s['lbl']))
        story.append(Spacer(1, 10))
        if snaps:
            buf = self._chart_intraday(snaps, par, cw)
            story.append(Image(buf, width=cw, height=cw*2.8/7.22))
        story.append(Spacer(1, 22))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=10))

        # ── RÉSUMÉ BAS DE PAGE ────────────────────────────────────────
        te_str   = f'TE {te:.2f}%  ·  ' if te else ''
        reb_str  = f'Prochain rebalancement le {next_r_str} ({days_r}j)' if days_r is not None else f'Prochain rebalancement {next_r_str}'
        brvm_str = f'BRVM30 {float(brvm_now):.2f}' if brvm_now else ''
        summary  = f'{n_parts:,} parts  ·  {aum:,.1f} M FCFA actif net  ·  {te_str}{reb_str}  ·  {brvm_str}'
        story.append(Paragraph(summary, s['summary']))
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=5))
        story.append(Paragraph(
            f'CGF Gestion — Agréé CREPMF  ·  OPCVM indiciel coté (ETF) Distribuant  ·  '
            f'Indice de référence : BRVM30 indice de cours  ·  '
            f'Généré le {datetime.now().strftime("%d/%m/%Y %H:%M")} UTC  ·  '
            f'Source : sikafinance.com  ·  Les performances passées ne préjugent pas des performances futures.',
            s['note']))

        # ══ PAGE 2 ════════════════════════════════════════════════════
        story.append(PageBreak())
        story += self._header(cw, etf_name, _date_fr(report_date))
        story.append(Spacer(1, 18))

        # ── Base 100 ──────────────────────────────────────────────────
        nd = sum(1 for d,pts in ih.items() if pts and pd.Timestamp(d) >= lt)
        story.append(Paragraph(
            f'PERFORMANCE RELATIVE  ·  BASE 100 AU {_dfr(launch_date)}  ·  {nd} séance(s)', s['lbl']))
        story.append(Spacer(1, 8))
        if nd >= 1:
            buf2 = self._chart_base100(ih, launch_date, brvm_h, brvm_al, par, cw)
            story.append(Image(buf2, width=cw, height=cw*1.15/7.22))
        story.append(Spacer(1, 18))
        story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=12))

        # ── Tableau portefeuille ──────────────────────────────────────
        story.append(Paragraph(
            f'COMPOSITION DU PORTEFEUILLE  ·  {len(basket)} titres  ·  NAV {nl.get("calc_date","—")}', s['lbl']))
        story.append(Spacer(1, 8))

        cws = [cw*x for x in [0.10, 0.08, 0.12, 0.095, 0.12, 0.09, 0.395]]
        hrow = [Paragraph(h, s['th']) for h in ['Ticker','Poids','Clôture J-1','Var. J','Cours live','Qté','Valeur (M FCFA)']]
        rows = [hrow]; vars_ = []
        for r in basket:
            tk = r['ticker'].upper()
            sk = sika.get(tk, {})
            vj = sk.get('variation') if isinstance(sk,dict) else None
            co = sk.get('dernier')   if isinstance(sk,dict) else None
            qt = r.get('quantite') or r.get('qty') or '—'
            vars_.append(vj)
            rows.append([
                Paragraph(tk, s['td']),
                Paragraph(f"{r['poids_pct']:.2f}%", s['td']),
                Paragraph(f"{int(r['dernier_prix']):,}" if r.get('dernier_prix') else '—', s['td']),
                Paragraph(f'{vj:+.2f}%' if vj is not None else '—',
                          s['tdg'] if (vj or 0)>0 else (s['tdr'] if (vj or 0)<0 else s['td'])),
                Paragraph(f"{int(co):,}" if co else '—', s['td']),
                Paragraph(f"{int(qt):,}" if isinstance(qt,(int,float)) else str(qt), s['td']),
                Paragraph(f"{r['pv_mfcfa']:.2f}", s['td']),
            ])

        sc = [
            ('BACKGROUND', (0,0),(-1,0), NAVY),
            ('LINEBELOW',  (0,0),(-1,0), 1.0, GOLD),
            ('ALIGN',      (0,0),(-1,-1), 'CENTER'),
            ('VALIGN',     (0,0),(-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0),(-1,-1), 1),
            ('BOTTOMPADDING',(0,0),(-1,-1),1),
            ('LINEABOVE',  (0,1),(-1,-1), 0.2, RULE),
        ]
        for i in range(1, len(rows)):
            if i%2==0: sc.append(('BACKGROUND',(0,i),(-1,i),LGRAY))
        pt = Table(rows, colWidths=cws, repeatRows=1)
        pt.setStyle(TableStyle(sc))
        story.append(pt)
        story.append(Spacer(1, 16))

        # ── Top mouvements ────────────────────────────────────────────
        bv_ = [(r['ticker'].upper(), r['poids_pct'], sika.get(r['ticker'].upper(),{}).get('variation'))
               for r in basket
               if isinstance(sika.get(r['ticker'].upper(),{}),dict)
               and sika.get(r['ticker'].upper(),{}).get('variation') is not None]
        if bv_:
            top5 = sorted(bv_, key=lambda x:x[2], reverse=True)[:5]
            bot5 = sorted(bv_, key=lambda x:x[2])[:5]
            hw = (cw-12)/2
            mk_h = lambda t: Paragraph(t, ParagraphStyle('mh', fontName='Helvetica-Bold',
                             fontSize=7.5, textColor=WHITE, alignment=TA_CENTER, leading=10))
            def mk_rows(data, col_style):
                rows = [[mk_h('Ticker'), mk_h('Poids'), mk_h('Variation')]]
                for tk, pds, vj in data:
                    rows.append([Paragraph(tk, s['td']),
                                  Paragraph(f'{pds:.2f}%', s['td']),
                                  Paragraph(f'{vj:+.2f}%', col_style)])
                return rows
            t_top = Table(mk_rows(top5, s['tdg']), colWidths=[hw*0.34]*3)
            t_bot = Table(mk_rows(bot5, s['tdr']), colWidths=[hw*0.34]*3)
            for t, bg in [(t_top, GREEN), (t_bot, RED)]:
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0,0),(-1,0), bg),
                    ('ALIGN',      (0,0),(-1,-1), 'CENTER'),
                    ('VALIGN',     (0,0),(-1,-1), 'MIDDLE'),
                    ('TOPPADDING', (0,0),(-1,-1), 3),
                    ('BOTTOMPADDING',(0,0),(-1,-1),3),
                    ('LINEABOVE',  (0,1),(-1,-1), 0.2, RULE),
                ]))
            lh = Paragraph('TOP 5 HAUSSES', ParagraphStyle('lh', fontName='Helvetica-Bold',
                           fontSize=7, textColor=GREEN, leading=9, spaceAfter=4))
            lb = Paragraph('TOP 5 BAISSES', ParagraphStyle('lb', fontName='Helvetica-Bold',
                           fontSize=7, textColor=RED,   leading=9, spaceAfter=4))
            movers = Table([[lh, lb], [t_top, t_bot]], colWidths=[hw, hw])
            movers.setStyle(TableStyle([
                ('VALIGN', (0,0),(-1,-1), 'TOP'),
                ('TOPPADDING', (0,0),(-1,-1), 0),
                ('BOTTOMPADDING',(0,0),(-1,-1),0),
                ('LEFTPADDING', (0,0),(-1,-1), 0),
                ('RIGHTPADDING',(0,0),(0,-1),  12),
                ('RIGHTPADDING',(1,0),(1,-1),   0),
            ]))
            story.append(KeepTogether([
                Paragraph('TOP MOUVEMENTS DU JOUR', s['lbl']),
                Spacer(1, 8),
                movers,
                Spacer(1, 12),
                HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=5),
                Paragraph(
                    f'Document réglementaire à usage interne  ·  CGF Gestion — Agréé CREPMF  ·  '
                    f'Généré le {datetime.now().strftime("%d/%m/%Y %H:%M")} UTC  ·  Source : sikafinance.com',
                    s['note']),
            ]))
        else:
            story.append(Spacer(1, 12))
            story.append(HRFlowable(width=cw, thickness=0.4, color=RULE, spaceAfter=5))
            story.append(Paragraph(
                f'Document réglementaire à usage interne  ·  CGF Gestion — Agréé CREPMF  ·  '
                f'Généré le {datetime.now().strftime("%d/%m/%Y %H:%M")} UTC  ·  Source : sikafinance.com',
                s['note']))

        doc.build(story)
        print(f'PDF : {pdf_path}')
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
