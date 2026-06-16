"""
scrape_brvm_history.py — Scrape TOUS les PDFs historiques de composition BRVM30
================================================================================
Lit _brvm_pdfs_found.json, déduplique par date, traite chaque PDF par OCR
et met à jour brvm_composition_history.json.

Usage:
    python scrape_brvm_history.py           # traite uniquement les nouveaux
    python scrape_brvm_history.py --force   # retraite tout
    python scrape_brvm_history.py --dry-run # liste sans traiter
"""
import sys, io, os, re, json, time, argparse, warnings
warnings.filterwarnings('ignore')

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import requests
from datetime import datetime

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
COMP_HISTORY = os.path.join(BASE_DIR, 'brvm_composition_history.json')
COMP_LATEST  = os.path.join(BASE_DIR, 'brvm_composition_latest.json')
PDFS_LIST    = os.path.join(BASE_DIR, '_brvm_pdfs_found.json')

BRVM30_SIZE  = 30

KNOWN_BRVM_TICKERS: frozenset = frozenset({
    'ABJC', 'BICB', 'BICC', 'BNBC', 'BOAB', 'BOABF', 'BOAC', 'BOAM', 'BOAN', 'BOAS',
    'CABC', 'CBIBF', 'CFAC', 'CIEC', 'ECOC', 'ETIT', 'FTSC', 'LNBB', 'NEIC', 'NSBC',
    'NTLC', 'ONTBF', 'ORAC', 'ORGT', 'PALC', 'PRSC', 'SAFC', 'SCRC', 'SDCC', 'SDSC',
    'SEMC', 'SGBC', 'SHEC', 'SIBC', 'SICC', 'SIVC', 'SLBC', 'SMBC', 'SNTS', 'SOGC',
    'SPHC', 'STAC', 'STBC', 'SVOC', 'TTLC', 'TTLS', 'UNLC', 'UNXC',
})

HTTP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'fr-FR,fr;q=0.9',
}

_ocr_reader = None

def _get(url):
    return requests.get(url, headers=HTTP_HEADERS, verify=False, timeout=60)

def _get_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        print("  [OCR] Initialisation EasyOCR (première fois, ~30s)...")
        _ocr_reader = easyocr.Reader(['fr', 'en'], verbose=False)
    return _ocr_reader

def _pdf_to_images(pdf_bytes, scale=3.0):
    import pypdfium2
    doc = pypdfium2.PdfDocument(pdf_bytes)
    images = []
    for i in range(len(doc)):
        page   = doc[i]
        bitmap = page.render(scale=scale)
        images.append(bitmap.to_numpy())
    return images

def _ocr_images(images):
    """OCR chaque page séparément, retourne liste de listes de (bbox, text, conf)."""
    reader = _get_reader()
    return [reader.readtext(img) for img in images]


def _parse_composition(ocr_per_page):
    """
    Extraction robuste basée sur la position spatiale.

    Observation clé sur les PDFs BRVM30 : les tickers sont TOUJOURS dans la
    colonne de droite (x ≈ 75-85 % de la largeur de page), à conf élevée.
    On extrait directement tous les tickers valides dans cette zone et on les
    trie par y (ordre du tableau). Beaucoup plus fiable que d'ancrer sur les
    numéros de ligne (qui sont parfois mal détectés).

    Page 2 : SORTANTS / ENTRANTS extraits par mots-clés.
    """
    def _valid(tok):
        return tok in KNOWN_BRVM_TICKERS

    # ── Page 1 : composition ─────────────────────────────────────────────────
    page1_results = ocr_per_page[0] if ocr_per_page else []
    blocks_p1 = []
    for (bbox, text, conf) in page1_results:
        if conf > 0.15:
            t  = text.strip().upper()
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            blocks_p1.append({'x': sum(xs)/4, 'y': sum(ys)/4,
                               'text': t, 'conf': conf})

    w_page = max((b['x'] for b in blocks_p1), default=1000)

    # Tickers valides dans la colonne droite (60–95 % de la largeur)
    right_tickers = [
        b for b in blocks_p1
        if _valid(b['text']) and 0.60 <= b['x'] / w_page <= 0.95
    ]

    # Dédupliquer : si même ticker à des y très proches (< 20 px), garder le plus confiant
    right_tickers.sort(key=lambda b: b['y'])
    deduped = []
    for b in right_tickers:
        if deduped and b['text'] == deduped[-1]['text'] and abs(b['y'] - deduped[-1]['y']) < 20:
            if b['conf'] > deduped[-1]['conf']:
                deduped[-1] = b
        else:
            deduped.append(b)

    # Prendre les 30 premiers (triés par y = ordre du tableau)
    composition = [b['text'] for b in deduped[:BRVM30_SIZE]]

    # Fallback si colonne droite insuffisante : élargir à x > 50 %
    if len(composition) < 25:
        wide_tickers = sorted(
            [b for b in blocks_p1 if _valid(b['text']) and b['x'] / w_page > 0.50],
            key=lambda b: b['y'],
        )
        seen = set()
        composition = []
        for b in wide_tickers:
            if b['text'] not in seen:
                seen.add(b['text'])
                composition.append(b['text'])
            if len(composition) == BRVM30_SIZE:
                break

    # ── Page 2 : entrées / sorties ───────────────────────────────────────────
    entries, exits = [], []
    all_pages_raw = []
    for page_results in ocr_per_page:
        for (bbox, text, conf) in page_results:
            if conf > 0.15:
                all_pages_raw.append(text.strip().upper())

    mode = 'search'
    for t in all_pages_raw:
        if re.search(r'SORTANT', t):
            mode = 'exits'; continue
        if re.search(r'ENTRANT', t):
            mode = 'entries'; continue
        if re.search(r'FAIT.{0,10}ABIDJAN', t):
            break
        if mode == 'exits'   and _valid(t) and t not in exits:   exits.append(t)
        if mode == 'entries' and _valid(t) and t not in entries: entries.append(t)

    return {'composition': composition, 'entries': entries, 'exits': exits,
            'n_tickers': len(composition)}


def _load_history():
    if not os.path.exists(COMP_HISTORY):
        return []
    with open(COMP_HISTORY, encoding='utf-8') as f:
        return json.load(f)


def _save_history(history):
    history = sorted(history, key=lambda x: x.get('rebal_date', ''))
    with open(COMP_HISTORY, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    # Latest = most recent with rebal_date
    dated = [h for h in history if h.get('rebal_date')]
    if dated:
        with open(COMP_LATEST, 'w', encoding='utf-8') as f:
            json.dump(dated[-1], f, ensure_ascii=False, indent=2)


def _process_pdf(pdf_info):
    """Télécharge, OCR et parse un PDF. Retourne le résultat ou None si erreur."""
    slug    = pdf_info['slug']
    pdf_url = pdf_info['pdf_url']
    sort_d  = pdf_info['sort_date']  # YYYYMMDD
    rebal_date = f"{sort_d[:4]}-{sort_d[4:6]}-{sort_d[6:8]}"

    print(f"  Téléchargement {slug}...")
    try:
        resp      = _get(pdf_url)
        pdf_bytes = resp.content
        print(f"    {len(pdf_bytes)//1024} Ko")
    except Exception as e:
        print(f"    [ERREUR] Download: {e}")
        return None

    print(f"    Rendu PDF → images...")
    try:
        images = _pdf_to_images(pdf_bytes, scale=3.0)
        print(f"    {len(images)} page(s)")
    except Exception as e:
        print(f"    [ERREUR] PDF→images: {e}")
        return None

    print(f"    OCR...")
    try:
        ocr_per_page = _ocr_images(images)
        total_blocs  = sum(len(p) for p in ocr_per_page)
        print(f"    {total_blocs} blocs ({len(ocr_per_page)} pages)")
    except Exception as e:
        print(f"    [ERREUR] OCR: {e}")
        return None

    parsed = _parse_composition(ocr_per_page)
    n = parsed['n_tickers']
    status = "OK" if n == BRVM30_SIZE else f"ALERTE ({n} tickers seulement)"
    print(f"    {status} — entrées: {parsed['entries']} sorties: {parsed['exits']}")

    return {
        'rebal_date':  rebal_date,
        'scrape_ts':   datetime.now().strftime('%Y-%m-%d %H:%M'),
        'pdf_slug':    slug,
        'pdf_url':     pdf_url,
        'article_url': pdf_info.get('article_url', ''),
        'n_tickers':   n,
        'composition': parsed['composition'],
        'entries':     parsed['entries'],
        'exits':       parsed['exits'],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--force',   action='store_true', help='Retraiter tous les PDFs')
    parser.add_argument('--dry-run', action='store_true', help='Lister sans traiter')
    args = parser.parse_args()

    if not os.path.exists(PDFS_LIST):
        print(f"Fichier {PDFS_LIST} introuvable. Lance d'abord _list_brvm_pdfs.py")
        sys.exit(1)

    with open(PDFS_LIST, encoding='utf-8') as f:
        all_pdfs = json.load(f)

    # Dédupliquer : garder un seul PDF par date (sans suffixe _0/_2 de préférence)
    by_date = {}
    for p in sorted(all_pdfs, key=lambda x: (x['sort_date'], len(x['slug']))):
        d = p['sort_date']
        if d not in by_date:
            by_date[d] = p
    unique_pdfs = sorted(by_date.values(), key=lambda x: x['sort_date'])

    print(f"PDFs uniques à traiter : {len(unique_pdfs)}")
    for p in unique_pdfs:
        print(f"  {p['sort_date']} — {p['slug']}")

    if args.dry_run:
        return

    history     = _load_history()
    # Indexer l'historique existant par rebal_date (pour skip)
    done_dates  = {h['rebal_date'] for h in history if h.get('rebal_date') and h.get('n_tickers', 0) >= 25}
    done_slugs  = {h.get('pdf_slug') for h in history}

    print(f"\nDates déjà traitées : {sorted(done_dates)}\n")

    processed = 0
    for pdf_info in unique_pdfs:
        d = pdf_info['sort_date']
        rebal_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

        if not args.force and rebal_date in done_dates:
            print(f"[SKIP] {rebal_date} — déjà dans l'historique")
            continue

        print(f"\n[TRAITEMENT] {rebal_date}")
        result = _process_pdf(pdf_info)
        if result is None:
            print(f"  [ECHEC] {rebal_date} ignoré")
            continue

        # Mettre à jour l'historique (remplacer si même date)
        history = [h for h in history if h.get('rebal_date') != rebal_date]
        history.append(result)
        _save_history(history)
        print(f"  Sauvegardé. Historique : {len(history)} entrées")
        processed += 1
        time.sleep(1)  # politesse serveur

    print(f"\n{'='*50}")
    print(f"Terminé. {processed} nouveaux PDFs traités.")
    print(f"Total historique : {len(_load_history())} entrées")

    # Résumé final
    hist = _load_history()
    dated = sorted([h for h in hist if h.get('rebal_date') and h.get('n_tickers', 0) >= 25],
                   key=lambda x: x['rebal_date'])
    print(f"\nCompositions disponibles ({len(dated)}) :")
    for h in dated:
        ent = len(h.get('entries', []))
        ext = len(h.get('exits',   []))
        mvt = f"+{ent}/-{ext}" if ent or ext else "stable"
        print(f"  {h['rebal_date']} — {h['n_tickers']} tickers — {mvt}")


if __name__ == '__main__':
    os.chdir(BASE_DIR)
    main()
