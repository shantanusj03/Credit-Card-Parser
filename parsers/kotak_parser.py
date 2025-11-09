# parsers/kotak_parser.py
import re
import io
from .pdf_utils import extract_text_pymupdf

# regexes
DATE_RE1 = re.compile(r'(\d{2}[\/\-]\d{2}[\/\-]\d{4})')                         # dd/mm/yyyy or dd-mm-yyyy
DATE_RE2 = re.compile(r'(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})')                      # 7-Sep-2024 or 07 Sep 2024
DATE_RANGE_RE = re.compile(r'from\s+(\d{1,2}[\/\-][A-Za-z0-9\-\/]{1,30}?)\s*(?:to|-)\s*(\d{1,2}[\/\-][A-Za-z0-9\-\/]{1,30}?)', re.I)
AMOUNT_RE = re.compile(r'([₹Rs.\s-]*)([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)', re.I)

def _norm_amount(tok: str):
    if tok is None:
        return None
    s = str(tok)
    s = s.replace('₹', '').replace('Rs.', '').replace('Rs', '').replace(',', '').strip()
    try:
        return f"{float(s):.2f}"
    except:
        # fallback: strip non-digits
        s2 = re.sub(r'[^\d.]', '', s)
        if not s2:
            return None
        try:
            return f"{float(s2):.2f}"
        except:
            return None

def _find_nearby_value(lines, idx, value_type='amount', forward=4, back=2):
    """
    Search lines near index `idx`:
      - same line first (lines[idx])
      - next forward lines up to `forward`
      - then previous up to `back`
    value_type: 'amount' or 'date'
    """
    N = len(lines)
    candidates = []
    # same line
    candidates.append(lines[idx])
    # forward lines
    for i in range(1, forward+1):
        if idx + i < N:
            candidates.append(lines[idx + i])
    # backward lines
    for i in range(1, back+1):
        if idx - i >= 0:
            candidates.append(lines[idx - i])

    for ln in candidates:
        ln = ln.strip()
        if not ln:
            continue
        if value_type == 'amount':
            m = AMOUNT_RE.search(ln)
            if m:
                return _norm_amount(m.group(2))
        else:
            # try dd/mm/yyyy first
            m1 = DATE_RE1.search(ln)
            if m1:
                return m1.group(1)
            m2 = DATE_RE2.search(ln)
            if m2:
                return m2.group(1)
    return None

def parse_kotak(file_obj):
    """
    Robust Kotak parser that maps labels to nearby tokens.
    Returns dict:
      bank, card_last4, statement_date, statement_period, due_date,
      total_due, minimum_due, transactions
    """
    # Accept file-like/bytes/path
    if isinstance(file_obj, (bytes, bytearray)):
        file_io = io.BytesIO(file_obj)
    else:
        file_io = file_obj

    text_all = extract_text_pymupdf(file_io)
    # normalize line endings and strip extra spaces
    text_all = text_all.replace('\r', '\n')
    # collapse multiple spaces but keep line breaks
    text_all = re.sub(r'[ \t]+', ' ', text_all)
    lines = [ln.rstrip() for ln in text_all.splitlines()]

    # prepare result
    card_last4 = None
    statement_date = None
    statement_period = None
    due_date = None
    total_due = None
    minimum_due = None

    # 1) Card last 4: look for "Primary Card Number" line
    for i, ln in enumerate(lines):
        if 'primary card number' in ln.lower():
            # extract last 4 digits anywhere on that line or next
            m = re.search(r'(\d{4})\s*$', ln)
            if m:
                card_last4 = m.group(1)
                break
            # try next few lines
            for j in range(1, 4):
                if i + j < len(lines):
                    m2 = re.search(r'(\d{4})\s*$', lines[i+j])
                    if m2:
                        card_last4 = m2.group(1)
                        break
            if card_last4:
                break
    # fallback: any pattern like 'XXXX 8314' anywhere
    if not card_last4:
        for ln in lines[:200]:
            m = re.search(r'X{2,}(\d{4})', ln)
            if m:
                card_last4 = m.group(1)
                break

    # 2) Statement Date label
    for i, ln in enumerate(lines[:200]):  # header is near top
        if 'statement date' in ln.lower():
            val = _find_nearby_value(lines, i, 'date', forward=3, back=2)
            if val:
                statement_date = val
            else:
                # sometimes date is on same paragraph but later token
                m = DATE_RE1.search(ln) or DATE_RE2.search(ln)
                if m:
                    statement_date = m.group(1)
            break

    # 3) Statement period (start date) — search for the "Date Transaction details from .. to .." pattern
    # search whole text for "from X to Y" style
    m_range = DATE_RANGE_RE.search(text_all)
    if m_range:
        # the regex used may capture tokens like '21-Jul-2024' etc; normalize start date if it's dd/mm/yyyy else keep as-is
        statement_period = m_range.group(1).strip()
        # convert dd-mm-yyyy with month names? We keep as found (user asked for start date)
    else:
        # fallback: detect first transaction date range phrase
        for ln in lines:
            if 'date transaction details' in ln.lower() and ('to' in ln.lower() or '-' in ln):
                m = DATE_RE1.search(ln) or DATE_RE2.search(ln)
                if m:
                    # find first date in that line
                    statement_period = m.group(1)
                    break

    # 4) Find labels for Minimum Amount Due, Total Amount Due, Remember to Pay By
    label_map = {
        "minimum": ["minimum amount due", "minimum amount", "minimum due"],
        "total": ["total amount due", "total amount", "total dues"],
        "remember": ["remember to pay by", "remember to pay", "pay by", "remember to pay by date"]
    }
    # iterate through lines to find label indices
    label_indices = {}
    for i, ln in enumerate(lines[:260]):  # header area
        lnl = ln.lower()
        for key, variants in label_map.items():
            for v in variants:
                if v in lnl:
                    # record first occurrence
                    if key not in label_indices:
                        label_indices[key] = i
    # Use _find_nearby_value to locate actual tokens
    if 'minimum' in label_indices:
        minimum_due = _find_nearby_value(lines, label_indices['minimum'], 'amount', forward=4, back=2)
    if 'total' in label_indices:
        total_due = _find_nearby_value(lines, label_indices['total'], 'amount', forward=4, back=2)
    if 'remember' in label_indices:
        # due date is a date token
        due_date = _find_nearby_value(lines, label_indices['remember'], 'date', forward=5, back=2)
        # also try to catch dd-mon-yyyy format
        if not due_date:
            # check same line or forward lines for dd-mon-yyyy patterns
            for j in range(0, 6):
                idx = label_indices['remember'] + j
                if idx >= len(lines): break
                ln2 = lines[idx]
                m1 = DATE_RE1.search(ln2) or DATE_RE2.search(ln2)
                if m1:
                    due_date = m1.group(1)
                    break

    # 5) If any still missing, fallback: search header area tokens ordered by proximity
    header_region = "\n".join(lines[:260])
    if not total_due:
        m = re.search(r'Total Amount Due[^\d\n\r]*([0-9,]+\.\d{2})', header_region, re.I)
        if m:
            total_due = _norm_amount(m.group(1))
    if not minimum_due:
        m = re.search(r'Minimum Amount Due[^\d\n\r]*([0-9,]+\.\d{2})', header_region, re.I)
        if m:
            minimum_due = _norm_amount(m.group(1))

    # Extra fallback: collect amount-like tokens in header and map heuristically
    if (not total_due or not minimum_due):
        amt_tokens = []
        for ln in lines[:260]:
            m = AMOUNT_RE.search(ln)
            if m:
                val = _norm_amount(m.group(2))
                if val and val not in amt_tokens:
                    amt_tokens.append(val)
        # heuristic: in header, smallest is often minimum, next larger is total (not perfect but helpful)
        if amt_tokens:
            try:
                # convert to floats for sorting
                fs = sorted([(float(x), x) for x in amt_tokens], key=lambda z: z[0])
                if not minimum_due:
                    minimum_due = fs[0][1]
                if not total_due and len(fs) > 1:
                    total_due = fs[1][1]
            except:
                pass

    # 6) Transactions: scan all pages for lines that start with dd/mm/yyyy
    transactions = []
    # We'll reuse the full text and parse page-by-page by splitting on 'Page' markers or simply process lines
    for ln in lines:
        ln_strip = ln.strip()
        if not ln_strip:
            continue
        mdate = DATE_RE1.match(ln_strip)
        if mdate:
            tdate = mdate.group(1)
            # find trailing amount
            mamt = re.search(r'([0-9,]+\.\d{2})\s*(Cr)?\s*$', ln_strip, re.I)
            if mamt:
                amount = _norm_amount(mamt.group(1))
                credit = bool(mamt.group(2))
                desc = re.sub(r'^\s*\d{2}[\/\-]\d{2}[\/\-]\d{4}\s*', '', ln_strip)
                desc = re.sub(r'([0-9,]+\.\d{2})\s*(Cr)?\s*$', '', desc, flags=re.I).strip()
                desc = re.sub(r'\s{2,}', ' ', desc)
                transactions.append({
                    "date": tdate,
                    "description": desc,
                    "amount": amount,
                    "credit": credit
                })

    # Pack results
    result = {
        "bank": "Kotak Mahindra Bank",
        "card_last4": card_last4,
        "statement_date": statement_date,
        "statement_period": statement_period,
        "due_date": due_date,
        "total_due": total_due,
        "minimum_due": minimum_due,
        "transactions": transactions
    }
    return result
