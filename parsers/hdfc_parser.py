# parsers/hdfc_parser.py
import re
import io
import fitz  # PyMuPDF

DATE_RE = re.compile(
    r'(\d{2}/\d{2}/\d{4}|[A-Za-z]{3,}\s+\d{1,2},\s*\d{4})'
)
AMOUNT_RE = re.compile(r'^[₹\sRsINR\.,]*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)\s*(?:CR|Dr)?$',
                       re.IGNORECASE)

def _clean_amount(s):
    if not s:
        return None
    s = s.replace('₹', '').replace('`', '').replace('Rs', '').replace('INR', '')
    s = s.replace(',', '').strip()
    s = re.sub(r'\s*(CR|Dr|DR)\s*$', '', s, flags=re.I)
    try:
        return f"{float(s):.2f}"
    except Exception:
        return None

def _open_doc(file_obj):
    # Supports BytesIO, bytes, or path
    if hasattr(file_obj, "read"):
        content = file_obj.read()
        return fitz.open(stream=content, filetype="pdf")
    if isinstance(file_obj, (bytes, bytearray)):
        return fitz.open(stream=bytes(file_obj), filetype="pdf")
    return fitz.open(file_obj)

def _collect_spans(page):
    """Return list of spans: dict(text, x0, y0, x1, y1)"""
    spans = []
    d = page.get_text("dict")
    for b in d.get("blocks", []):
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                txt = (s.get("text") or "").strip()
                if txt:
                    x0, y0, x1, y1 = s["bbox"]
                    spans.append({
                        "text": txt,
                        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "cx": (x0 + x1) / 2.0
                    })
    return spans

def _nearest_value_below(spans, header_span, val_kind="amount",
                         max_vdist=80, x_tol=40):
    """
    Find the closest span below header_span in the same column.
    val_kind: "amount" uses AMOUNT_RE, "date" uses DATE_RE
    max_vdist: vertical distance cap in points
    x_tol: allow some horizontal drift
    """
    hx0, hx1 = header_span["x0"], header_span["x1"]
    hcx, hy1 = header_span["cx"], header_span["y1"]
    best = None
    best_dy = 1e9
    for s in spans:
        if s["y0"] <= hy1:        # must be below header cell
            continue
        # roughly same column (center under header; allow tolerance)
        if not (hx0 - x_tol <= s["cx"] <= hx1 + x_tol):
            continue
        if s["y0"] - hy1 > max_vdist:   # too far down (next widgets/sections)
            continue
        txt = s["text"]
        if val_kind == "amount":
            if not AMOUNT_RE.match(txt):
                continue
        else:
            if not DATE_RE.search(txt):
                continue
        dy = s["y0"] - hy1
        if dy < best_dy:
            best = s
            best_dy = dy
    return best

def parse_hdfc(file_obj):
    """
    Layout-aware HDFC parser (works for table header in screenshot-style PDFs).
    Returns:
      bank, card_last4, statement_date, statement_period, due_date,
      total_due, minimum_due, transactions (basic)
    """
    doc = _open_doc(file_obj)
    if doc.page_count == 0:
        return {"bank": "HDFC Bank", "error": "Empty PDF"}
    page0 = doc[0]
    spans = _collect_spans(page0)

    # ---------- Statement Date ----------
    statement_date = None
    for s in spans:
        if "statement date" in s["text"].lower():
            # search to the right on same row or next spans
            row_y = s["y0"]
            candidates = [t for t in spans if abs(t["y0"] - row_y) <= 6 or (0 <= t["y0"] - s["y1"] <= 20)]
            flat = " ".join([t["text"] for t in sorted(candidates, key=lambda z: z["x0"])])
            m = DATE_RE.search(flat)
            if m:
                statement_date = m.group(1)
                break
    # loose fallback anywhere
    if not statement_date:
        flat = " ".join([s["text"] for s in spans[:120]])
        m = DATE_RE.search(flat)
        if m:
            statement_date = m.group(1)

    # ---------- Card last 4 ----------
    card_last4 = None
    for s in spans:
        if "card no" in s["text"].lower():
            # read right on same row
            row = [t for t in spans if abs(t["y0"] - s["y0"]) <= 6]
            row = sorted(row, key=lambda z: z["x0"])
            row_txt = " ".join([t["text"] for t in row])
            # examples: 4695 25XX XXXX 3458
            m = re.search(r'(\d{4})\s*XX+.*?(\d{4})', row_txt, re.I)
            if m:
                card_last4 = m.group(2)
                break
            m = re.search(r'(\d{4})\s*(?:\d{2}X{2})\s*X{3,}\s*(\d{4})', row_txt, re.I)
            if m:
                card_last4 = m.group(2)
                break
            # generic fallback
            m = re.search(r'(\d{4})\s*$', row_txt)
            if m:
                card_last4 = m.group(1)
                break

    # ---------- Three headers in the orange band ----------
    header_map = {
        "due": re.compile(r'payment\s+due\s+date', re.I),
        "total": re.compile(r'^total\s+dues$', re.I),
        "min": re.compile(r'^minimum\s+amount\s+due$', re.I),
    }
    # find header spans
    hdr_spans = {}
    for s in spans:
        txt = s["text"].strip()
        for key, rx in header_map.items():
            if rx.search(txt):
                hdr_spans[key] = s
    # Find values directly below each header (same column)
    due_date = None
    total_due = None
    minimum_due = None
    if "due" in hdr_spans:
        v = _nearest_value_below(spans, hdr_spans["due"], val_kind="date", max_vdist=60, x_tol=25)
        if v:
            # extract the actual date from the span text
            m = DATE_RE.search(v["text"])
            if m:
                due_date = m.group(1)

    if "total" in hdr_spans:
        v = _nearest_value_below(spans, hdr_spans["total"], val_kind="amount", max_vdist=60, x_tol=25)
        if v:
            total_due = _clean_amount(AMOUNT_RE.match(v["text"]).group(1))

    if "min" in hdr_spans:
        v = _nearest_value_below(spans, hdr_spans["min"], val_kind="amount", max_vdist=60, x_tol=25)
        if v:
            minimum_due = _clean_amount(AMOUNT_RE.match(v["text"]).group(1))

    # ---------- Transactions (simple line-based; optional here) ----------
    # If you need HDFC transactions as well, keep your existing logic or ask me to add a bbox-based stitcher.
    transactions = []

    # ---------- Statement period (not always present) ----------
    statement_period = statement_date  # what you were displaying

    doc.close()
    return {
        "bank": "HDFC Bank",
        "card_last4": card_last4,
        "statement_date": statement_date,
        "statement_period": statement_period,
        "due_date": due_date,
        "total_due": total_due,
        "minimum_due": minimum_due,
        "transactions": transactions
    }
