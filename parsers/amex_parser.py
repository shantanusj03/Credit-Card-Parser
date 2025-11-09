# parsers/amex_parser.py
import re
import io
import fitz   # PyMuPDF

def _clean_amount(tok):
    """Normalize money strings like '$4,053.61' or '-$3,481.72' -> '4053.61' (string)"""
    if tok is None:
        return None
    s = str(tok)
    # remove currency symbols and parentheses, keep minus sign if present
    s = s.replace('$', '').replace('USD', '')
    s = s.replace('\u2013', '-')  # en-dash -> minus
    s = s.replace('\u2212', '-')  # minus sign
    s = s.strip()
    # handle parentheses like (3,481.72) -> -3481.72
    if s.startswith('(') and s.endswith(')'):
        s = '-' + s[1:-1]
    # remove commas and stray characters
    s = re.sub(r'[,\s]', '', s)
    # strip any non-digit except leading '-'' and decimal point
    s = re.sub(r'[^0-9\.\-]', '', s)
    if s in ('', '-', '.'):
        return None
    try:
        return f"{abs(float(s)):.2f}"  # return absolute value as string; credit flag indicates sign
    except:
        try:
            # fallback parse digits only
            digits = re.sub(r'[^0-9.]', '', s)
            return f"{float(digits):.2f}" if digits else None
        except:
            return None

def _open_doc(file_obj):
    """Open PDF from path / file-like / bytes"""
    if hasattr(file_obj, "read"):
        data = file_obj.read()
        return fitz.open(stream=data, filetype="pdf")
    if isinstance(file_obj, (bytes, bytearray)):
        return fitz.open(stream=bytes(file_obj), filetype="pdf")
    return fitz.open(file_obj)

def _extract_text(file_obj):
    """Return concatenated page text"""
    doc = _open_doc(file_obj)
    pages = []
    for p in doc:
        pages.append(p.get_text("text"))
    doc.close()
    text = "\n".join(pages)
    # normalize common noise
    text = text.replace('\r', '\n')
    text = text.replace('\u2022', ' ')  # bullets
    text = text.replace('\uf0b7', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return text

# Date regexes common in Amex statements: MM/DD/YY or MM/DD/YYYY or '05/28/24' or '05/28/2024'
DATE_SHORT = re.compile(r'(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})')

# Date range regex: "Transactions Dated From To" or "Transactions Dated From: 04/28/24 To: 05/28/24"
DATE_RANGE = re.compile(
    r'(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})\s*(?:to|-|TO|TO:)\s*(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})',
    re.I
)

# header label regexes
RE_CLOSING_DATE = re.compile(r'closing\s+date[:\s]*([0-9]{1,2}[\/-][0-9]{1,2}[\/-][0-9]{2,4})', re.I)
RE_ACCOUNT_ENDING = re.compile(r'account\s+ending[:\s]*([0-9\-Xx]{2,20})', re.I)
RE_PAYMENT_DUE = re.compile(r'payment\s+due\s+date[:\s]*([0-9]{1,2}[\/-][0-9]{1,2}[\/-][0-9]{2,4})', re.I)
RE_NEW_BALANCE = re.compile(r'new\s+balance[:\s]*\$?([0-9\-,]+\.\d{2})', re.I)
RE_AMOUNT_DUE = re.compile(r'amount\s+due[:\s]*\$?([0-9\-,]+\.\d{2})', re.I)

# transaction line regex: starts with date then text then amount at end
TX_LINE_RE = re.compile(
    r'^\s*(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})\s+(.+?)\s+(-?\$?[0-9,]+\.\d{2}|\([0-9,]+\.\d{2}\))\s*$',
    re.I
)

# many Amex statements also include lines where the posting date is followed by multiline description and amount on next line(s).
# We'll implement a small stateful scanner to collect those cases.


def parse_amex(file_obj):
    """
    Parse American Express statement PDF and return dict of extracted fields.
    Accepts path, bytes, or file-like.
    """
    text = _extract_text(file_obj)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # result placeholders
    card_last4 = None
    statement_date = None     # Closing date
    statement_period = None   # start date if a range found
    due_date = None
    total_due = None
    minimum_due = None
    transactions = []

    header_block = " ".join(lines[:200])  # search top area

    # 1) Header extraction (robust attempts)
    # Account ending
    m_acc = RE_ACCOUNT_ENDING.search(header_block)
    if m_acc:
        tok = m_acc.group(1)
        # try to find last 4 digits
        m4 = re.search(r'(\d{4})', tok)
        card_last4 = m4.group(1) if m4 else tok.strip()

    # Closing/Statement date
    m_close = RE_CLOSING_DATE.search(header_block)
    if m_close:
        statement_date = m_close.group(1)
    else:
        # Sometimes: 'Closing Date 05/28/24' split across lines — scan top lines
        for ln in lines[:60]:
            m = RE_CLOSING_DATE.search(ln)
            if m:
                statement_date = m.group(1)
                break

    # Payment Due Date (explicit)
    m_due = RE_PAYMENT_DUE.search(header_block)
    if m_due:
        due_date = m_due.group(1)
    else:
        # sometimes "Payment Due Date" and "Amount Due" are in different places; try line-by-line
        for ln in lines[:120]:
            m = RE_PAYMENT_DUE.search(ln)
            if m:
                due_date = m.group(1)
                break

    # New Balance or Amount Due (Total)
    m_bal = RE_NEW_BALANCE.search(header_block)
    if m_bal:
        total_due = _clean_amount(m_bal.group(1))
    else:
        m_amt = RE_AMOUNT_DUE.search(header_block)
        if m_amt:
            total_due = _clean_amount(m_amt.group(1))

    # Statement period (Transactions Dated From ... To ...)
    # search for a date-range in the header or anywhere in top pages
    m_range = DATE_RANGE.search(header_block)
    if not m_range:
        # try broader search first 500 lines text
        big_chunk = " ".join(lines[:500])
        m_range = DATE_RANGE.search(big_chunk)
    if m_range:
        statement_period = m_range.group(1)  # start date

    # Minimum due: Amex sometimes doesn't show a minimum (pay-in-full card). But check header for 'Minimum Amount Due'
    m_min = re.search(r'minimum\s+amount\s+due[:\s]*\$?([0-9\-,]+\.\d{2})', header_block, re.I)
    if m_min:
        minimum_due = _clean_amount(m_min.group(1))

    # If still missing total_due try 'New Balance $4,053.61' or 'New Balance' tokens in header lines
    if not total_due:
        for ln in lines[:120]:
            m = re.search(r'new\s+balance[:\s]*\$?([0-9\-,]+\.\d{2})', ln, re.I)
            if m:
                total_due = _clean_amount(m.group(1))
                break

    # 2) Transactions extraction
    # We'll scan through lines and use two strategies:
    #  - direct single-line pattern: "05/05/24 GOOGLE*KEVIN ... $4.99"
    #  - three-line pattern: "05/15/24" (line), "DELTA AIR LINES ATLANTA" (line), "$1,758.63" (line)
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        # prefer single-line TX pattern
        m_tx = TX_LINE_RE.match(ln)
        if m_tx:
            date_tok = m_tx.group(1)
            desc = m_tx.group(2).strip()
            amt_raw = m_tx.group(3).strip()
            # normalize amount and credit status
            credit = False
            if re.search(r'-\$', amt_raw) or amt_raw.startswith('-') or amt_raw.startswith('('):
                credit = True
            amt = _clean_amount(amt_raw)
            transactions.append({
                "date": date_tok,
                "description": desc,
                "amount": amt,
                "credit": credit
            })
            i += 1
            continue

        # if line is a date-only (like '04/19/24*' or '04/19/24* JANE BAUGHMAN ...' handled above),
        # handle when date and description/amount appear on next lines
        m_date_only = re.match(r'^\s*(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})(\*?)\s*$', ln)
        if m_date_only:
            date_tok = m_date_only.group(1)
            desc = ""
            amt = None
            credit = False
            # look ahead for description and amount in next few lines
            if i + 1 < n:
                next_ln = lines[i + 1]
                # if next line looks like an amount-only line (e.g. '$1,758.63' or '($1,234.56)')
                if re.match(r'^[\$\(]?[0-9,\-]+\.\d{2}\)?$', next_ln) or re.search(r'^[\$\(]?[0-9,\-]+\.\d{2}\)?$', next_ln.strip()):
                    amt = _clean_amount(next_ln)
                    desc = ""
                    i += 2
                else:
                    # take next line as description, then search the following lines for amount
                    desc = next_ln
                    found_amt = False
                    for j in range(2, 5):
                        if i + j < n:
                            cand = lines[i + j]
                            m_amt = re.search(r'([-\$\(]?[0-9,]+\.\d{2}\)?)', cand)
                            if m_amt:
                                amt_raw = m_amt.group(1)
                                if re.search(r'-\$', amt_raw) or amt_raw.startswith('-') or amt_raw.startswith('('):
                                    credit = True
                                amt = _clean_amount(amt_raw)
                                i += j
                                found_amt = True
                                break
                    if not found_amt:
                        # no trailing amount found — treat as description-only
                        i += 2
            else:
                i += 1
            transactions.append({
                "date": date_tok,
                "description": desc.strip(),
                "amount": amt,
                "credit": credit
            })
            continue

        # fallback: sometimes lines contain a description followed by an amount but not matching TX_LINE_RE
        # e.g., "05/07/24 UNITED AIRLINES HOUSTON TX ... 561.60" split slightly; catch those with trailing amount
        m_more = re.match(r'^(\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4})\s+(.+?)\s+([0-9,]+\.\d{2})$', ln)
        if m_more:
            date_tok = m_more.group(1)
            desc = m_more.group(2).strip()
            amt = _clean_amount(m_more.group(3))
            transactions.append({"date": date_tok, "description": desc, "amount": amt, "credit": False})
            i += 1
            continue

        i += 1

    # 3) final cleanups and fallbacks
    # If card_last4 not found in header_block, try to find it in whole file by "Account Ending 9-77002" or "Account Ending 9-77002"
    if not card_last4:
        m_acc2 = re.search(r'account\s+ending[:\s]*([0-9\-Xx]{2,20})', " ".join(lines), re.I)
        if m_acc2:
            mm = re.search(r'(\d{4})', m_acc2.group(1))
            card_last4 = mm.group(1) if mm else m_acc2.group(1)

    # If due_date still None, try looser patterns in top 200 lines
    if not due_date:
        for ln in lines[:200]:
            m = re.search(r'payment\s+due[:\s]*([A-Za-z0-9\/\-\s]+)', ln, re.I)
            if m:
                d = DATE_SHORT.search(m.group(1))
                if d:
                    due_date = d.group(1)
                    break

    # If statement_date still None, try 'Closing Date' loose search in top area
    if not statement_date:
        for ln in lines[:200]:
            m = re.search(r'closing\s+date[:\s]*([0-9]{1,2}[\/-][0-9]{1,2}[\/-][0-9]{2,4})', ln, re.I)
            if m:
                statement_date = m.group(1)
                break

    # Normalize dates (optional): user format kept as found (MM/DD/YY or MM/DD/YYYY)
    # If statement_period missing, try to derive from transactions: earliest transaction date
    if not statement_period and transactions:
        # pick earliest transaction date string (naive: the first transaction printed may be earliest or latest depending on ordering)
        # better to parse dates into canonical form, but we keep the raw token for now
        try:
            # attempt to find minimum by parsing mm/dd/yy
            def _to_tuple(dstr):
                m = re.match(r'(\d{1,2})[\/-](\d{1,2})[\/-](\d{2,4})', dstr)
                if not m: return (9999,9999,9999)
                mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if yy < 100: yy += 2000
                return (yy, mm, dd)
            dates = [t["date"] for t in transactions if t.get("date")]
            dates_parsed = sorted(dates, key=_to_tuple)
            if dates_parsed:
                statement_period = dates_parsed[0]
        except:
            pass

    # return result
    return {
        "bank": "American Express",
        "card_last4": card_last4,
        "statement_date": statement_date,
        "statement_period": statement_period,
        "due_date": due_date,
        "total_due": total_due,
        "minimum_due": minimum_due,
        "transactions": transactions
    }
