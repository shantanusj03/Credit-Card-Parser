# parsers/sbi_parser.py
import re
import io
import fitz            # PyMuPDF
from .pdf_utils import extract_text_pymupdf

# Regex helpers
DATE_DDMMYYYY = re.compile(r'(\d{2}[\/\-]\d{2}[\/\-]\d{4})')          # 23/07/2024
DATE_DD_MON_YYYY = re.compile(r'(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})')     # 6 Sep 2025 or 06 Sep 2025
DATE_RANGE = re.compile(r'(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s*(?:to|-)\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{4})', re.I)
AMOUNT_RE = re.compile(r'([₹Rs.\s-]*)([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)', re.I)

def _clean_amount(tok):
    if not tok:
        return None
    s = str(tok).replace('₹','').replace('Rs.','').replace('Rs','').replace(',','').strip()
    try:
        return f"{float(s):.2f}"
    except:
        s2 = re.sub(r'[^\d.]','', s)
        if not s2:
            return None
        try:
            return f"{float(s2):.2f}"
        except:
            return None

def _open_doc(file_obj):
    if hasattr(file_obj, "read"):
        data = file_obj.read()
        return fitz.open(stream=data, filetype="pdf")
    if isinstance(file_obj, (bytes, bytearray)):
        return fitz.open(stream=bytes(file_obj), filetype="pdf")
    return fitz.open(file_obj)

def _collect_spans(page):
    spans = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = (span.get("text") or "").strip()
                if not txt:
                    continue
                x0,y0,x1,y1 = span["bbox"]
                spans.append({
                    "text": txt,
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "cx": (x0 + x1) / 2.0
                })
    return spans

def _find_label_spans(spans, patterns):
    found = {}
    for s in spans:
        for key, rx in patterns.items():
            if rx.search(s["text"]):
                # prefer top-most occurrence
                if key not in found or s["y0"] < found[key]["y0"]:
                    found[key] = s
    return found

def _value_same_row_right(spans, header_span, kind="amount", y_tol=7, x_min_offset=2):
    hy = header_span["y0"]
    candidates = [s for s in spans if abs(s["y0"] - hy) <= y_tol and s["x0"] > header_span["x1"] + x_min_offset]
    candidates = sorted(candidates, key=lambda z: z["x0"])
    for s in candidates:
        t = s["text"].strip()
        if kind == "amount":
            if AMOUNT_RE.search(t):
                return s
        elif kind == "date":
            if DATE_DDMMYYYY.search(t) or DATE_DD_MON_YYYY.search(t):
                return s
        else:
            return s
    return None

def _nearest_value_below(spans, header_span, kind="amount", max_vdist=140, x_tol=90):
    hx0, hx1 = header_span["x0"], header_span["x1"]
    hy1 = header_span["y1"]
    best = None
    best_dy = float("inf")
    for s in spans:
        if s["y0"] <= hy1: continue
        # same column roughly
        if not (hx0 - x_tol <= s["cx"] <= hx1 + x_tol): continue
        dy = s["y0"] - hy1
        if dy > max_vdist: continue
        st = s["text"].strip()
        if kind == "amount":
            if AMOUNT_RE.search(st):
                if dy < best_dy:
                    best = s; best_dy = dy
        elif kind == "date":
            if DATE_DDMMYYYY.search(st) or DATE_DD_MON_YYYY.search(st):
                if dy < best_dy:
                    best = s; best_dy = dy
        else:
            if dy < best_dy:
                best = s; best_dy = dy
    return best

def _line_nearby_search(lines, idx, kind='amount', forward=4, back=2):
    n = len(lines)
    order = [idx] + [idx+i for i in range(1, forward+1) if idx+i < n] + [idx-i for i in range(1, back+1) if idx-i >= 0]
    for j in order:
        ln = lines[j].strip()
        if not ln: continue
        if kind == 'amount':
            m = AMOUNT_RE.search(ln)
            if m: return _clean_amount(m.group(2))
        else:
            m1 = DATE_DDMMYYYY.search(ln)
            if m1: return m1.group(1)
            m2 = DATE_DD_MON_YYYY.search(ln)
            if m2: return m2.group(1)
    return None

def parse_sbi(file_obj):
    # open doc (fitz) and also get plain text for line-based fallbacks
    doc = None
    try:
        doc = _open_doc(file_obj)
        text_pages = [p.get_text("text") for p in doc]
        full_text = "\n".join(text_pages)
    except Exception:
        # fallback to pdf_utils textual extraction
        full_text = extract_text_pymupdf(file_obj)
    # normalize
    full_text = full_text.replace('\r', '\n')
    full_text = re.sub(r'\u2022', ' ', full_text)   # bullets
    full_text = re.sub(r'[ \t]+', ' ', full_text)
    lines = [l.rstrip() for l in full_text.splitlines()]

    # results init
    card_last4 = None
    statement_date = None
    statement_period = None
    due_date = None
    total_due = None
    minimum_due = None
    transactions = []

    # header region lines to search
    header_region_lines = lines[:300]
    header_text = "\n".join(header_region_lines)

    # === NEW: explicit inline header regex for Payment Due Date ===
    # This captures cases like:
    # "Total Amount Due: ■20,089.00 Payment Due Date: 15 Oct 2025"
    m_inline_due = re.search(
        r'payment\s+due\s+date[:\s]*([0-9]{1,2}\s+[A-Za-z]{3}\s+\d{4}|\d{2}[\/\-]\d{2}[\/\-]\d{4})',
        header_text, re.I
    )
    if m_inline_due:
        due_date = m_inline_due.group(1)

    # FIRST: span-based label->value mapping (preferred) if doc opened
    if doc:
        spans = _collect_spans(doc[0])
        label_patterns = {
            "card_no": re.compile(r'primary\s+card\s+number|card\s+no|card\s+ending|card\s+number', re.I),
            "statement_date": re.compile(r'statement\s+date|statement\s+generated', re.I),
            "payment_due": re.compile(r'payment\s+due\s+date|payment\s+due|due\s+date|payment due', re.I),
            "total_due": re.compile(r'\btotal\s+amount\s+due\b|\btotal\s+due\b|\btotal\s+dues\b', re.I),
            "minimum_due": re.compile(r'\bminimum\s+amount\s+due\b|\bminimum\s+due\b|\bminimum\s+amount\b', re.I),
            "period_label": re.compile(r'statement\s+period', re.I),
        }
        found = _find_label_spans(spans, label_patterns)

        # Card last4
        if "card_no" in found:
            hdr = found["card_no"]
            v = _value_same_row_right(spans, hdr, kind=None)
            if v:
                m = re.search(r'(\d{4})\s*$', v["text"])
                if m: card_last4 = m.group(1)
            if not card_last4:
                # search in header spans vicinity
                vicinity = " ".join([s["text"] for s in spans if s["y0"] >= hdr["y0"]-3 and s["y0"] <= hdr["y1"]+12])
                m2 = re.search(r'(\d{4})\s*$', vicinity)
                if m2: card_last4 = m2.group(1)

        # Statement date
        if "statement_date" in found:
            hdr = found["statement_date"]
            v = _value_same_row_right(spans, hdr, kind="date")
            if not v: v = _nearest_value_below(spans, hdr, kind="date")
            if v:
                m = DATE_DDMMYYYY.search(v["text"]) or DATE_DD_MON_YYYY.search(v["text"])
                if m: statement_date = m.group(1)

        # Payment due (span-based) - only overwrite if not already found inline
        if due_date is None and "payment_due" in found:
            hdr = found["payment_due"]
            # prefer same-row right
            v = _value_same_row_right(spans, hdr, kind="date")
            if not v: v = _nearest_value_below(spans, hdr, kind="date")
            if v:
                m = DATE_DDMMYYYY.search(v["text"]) or DATE_DD_MON_YYYY.search(v["text"])
                if m: due_date = m.group(1)

        # total & minimum
        if "total_due" in found and total_due is None:
            hdr = found["total_due"]
            v = _value_same_row_right(spans, hdr, kind="amount")
            if not v: v = _nearest_value_below(spans, hdr, kind="amount")
            if v:
                mm = AMOUNT_RE.search(v["text"])
                if mm: total_due = _clean_amount(mm.group(2))
        if "minimum_due" in found and minimum_due is None:
            hdr = found["minimum_due"]
            v = _value_same_row_right(spans, hdr, kind="amount")
            if not v: v = _nearest_value_below(spans, hdr, kind="amount")
            if v:
                mm = AMOUNT_RE.search(v["text"])
                if mm: minimum_due = _clean_amount(mm.group(2))

        # statement period label case: try to grab right-side token or neighbor
        if "period_label" in found and statement_period is None:
            hdr = found["period_label"]
            v = _value_same_row_right(spans, hdr, kind="date")
            if v:
                m = DATE_DDMMYYYY.search(v["text"]) or DATE_DD_MON_YYYY.search(v["text"])
                if m:
                    statement_period = m.group(1)

    # SECOND: line-based fallbacks where span approach didn't find values
    # card_last4 fallback
    if not card_last4:
        for i, ln in enumerate(header_region_lines):
            low = ln.lower()
            if 'card' in low and any(w in low for w in ['ending', 'primary card', 'card no', 'card number']):
                m = re.search(r'(\d{4})\s*$', ln)
                if m:
                    card_last4 = m.group(1); break
                # check next lines
                for j in range(1,5):
                    if i+j < len(header_region_lines):
                        m2 = re.search(r'(\d{4})\s*$', header_region_lines[i+j])
                        if m2:
                            card_last4 = m2.group(1); break
            if card_last4: break

    # statement_date fallback
    if not statement_date:
        for i,ln in enumerate(header_region_lines):
            if 'statement date' in ln.lower() or 'statement generated' in ln.lower():
                dd = _line_nearby_search(header_region_lines, i, kind='date', forward=3, back=2)
                if dd:
                    statement_date = dd; break

    # statement_period from a date-range if exists anywhere
    if not statement_period:
        mr = DATE_RANGE.search(full_text)
        if mr:
            # prefer start-date as statement_period (user wanted start)
            statement_period = mr.group(1)
    # else look for explicit 'statement period' label
    if not statement_period:
        for i,ln in enumerate(header_region_lines):
            if 'statement period' in ln.lower():
                dd = _line_nearby_search(header_region_lines, i, kind='date', forward=3, back=2)
                if dd:
                    statement_period = dd; break

    # total & minimum fallbacks using lines
    if not total_due or not minimum_due:
        for i,ln in enumerate(header_region_lines):
            lnl = ln.lower()
            if not total_due and ('total amount due' in lnl or 'total due' in lnl or 'total dues' in lnl):
                v = _line_nearby_search(header_region_lines, i, kind='amount', forward=4, back=2)
                if v: total_due = v
            if not minimum_due and ('minimum amount due' in lnl or ('minimum' in lnl and 'amount' in lnl) or 'minimum due' in lnl):
                v = _line_nearby_search(header_region_lines, i, kind='amount', forward=4, back=2)
                if v: minimum_due = v
            if total_due and minimum_due:
                break

    # final header heuristics: collect amount-like tokens in header region and heuristically assign
    if (not total_due or not minimum_due):
        hdr_amts = []
        for ln in header_region_lines:
            mm = AMOUNT_RE.search(ln)
            if mm:
                val = _clean_amount(mm.group(2))
                if val and val not in hdr_amts:
                    hdr_amts.append(val)
        if hdr_amts:
            try:
                byval = sorted(hdr_amts, key=lambda x: float(x))
                if not minimum_due:
                    minimum_due = byval[0]
                if not total_due and len(byval)>1:
                    # pick the largest as total (safer than position heuristic)
                    total_due = byval[-1]
            except:
                if not total_due:
                    total_due = hdr_amts[0]
                if not minimum_due and len(hdr_amts)>1:
                    minimum_due = hdr_amts[1]

    # Transactions: line-based conservative parse
    tx_date_re = re.compile(r'^\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})\b')
    tx_amt_trail = re.compile(r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2}))\s*(Cr)?\s*$', re.I)
    for ln in full_text.splitlines():
        if not ln.strip(): continue
        mdate = tx_date_re.match(ln.strip())
        if mdate:
            tdate = mdate.group(1)
            mamt = tx_amt_trail.search(ln.strip())
            if mamt:
                raw = mamt.group(1)
                credit = bool(mamt.group(2))
                desc = re.sub(r'^\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})\s*', '', ln.strip())
                desc = re.sub(r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2}))\s*(Cr)?\s*$', '', desc).strip()
                transactions.append({
                    "date": tdate,
                    "description": re.sub(r'\s{2,}',' ', desc).strip(),
                    "amount": _clean_amount(raw),
                    "credit": credit
                })

    # Ensure we haven't accidentally merged label text into statement_period etc.
    # Final returned dict
    return {
        "bank": "SBI Card",
        "card_last4": card_last4,
        "statement_date": statement_date,
        "statement_period": statement_period,
        "due_date": due_date,
        "total_due": total_due,
        "minimum_due": minimum_due,
        "transactions": transactions
    }
