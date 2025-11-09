# parsers/icici_parser.py
import re
import io
from .pdf_utils import extract_text_pymupdf

def _clean_amount(s):
    if not s:
        return None
    s = s.replace('`','').replace('₹','').replace('Rs','').replace(',','').strip()
    return s

def parse_icici(file_obj):
    """
    Parse ICICI credit card statement.
    file_obj: file-like object (BytesIO or Flask file) or path.
    returns dict with keys:
      - bank
      - card_last4
      - statement_date
      - statement_period
      - due_date
      - total_due
      - minimum_due
      - transactions: list of {date, description, amount, credit_flag}
    """
   
    if isinstance(file_obj, (bytes, bytearray)):
        file_for_text = io.BytesIO(file_obj)
    else:
        file_for_text = file_obj

    text = extract_text_pymupdf(file_for_text)

    text_norm = text.replace('\r','\n')
    text_norm = re.sub(r'[ \t]+', ' ', text_norm)

    card_last4 = None

    m = re.search(r'(\d{4})X{2,}(\d{4})', text)
    if m:
        card_last4 = m.group(2)
    else:
        m = re.search(r'X{2,}(\d{4})', text)
        if m:
            card_last4 = m.group(1)
        else:
            # fallback: "Card Ending 4006"
            m = re.search(r'Card(?:\s+Ending|(?:\s+No\.?)|(?:\s+Number))[:\s]*\*?(\d{4})', text, re.I)
            if m:
                card_last4 = m.group(1)

    # 2) Statement date (single date heading)
    statement_date = None
    m = re.search(r'STATEMENT\s+DATE\s*\n\s*([A-Za-z0-9 ,\/\-]+)', text, re.I)
    if m:
        statement_date = m.group(1).strip()
    else:
        # sometimes appears as "Statement Date : June 22, 2024"
        m = re.search(r'Statement\s+Date\s*[:\-]\s*([A-Za-z0-9,\/\- ]+)', text, re.I)
        if m:
            statement_date = m.group(1).strip()

    # 3) Statement period (range)
    statement_period = None
    m = re.search(r'Statement\s+period\s*[:\-]\s*([A-Za-z0-9 ,\-to]+)', text, re.I)
    if m:
        statement_period = m.group(1).strip()

    # 4) Payment due date
    due_date = None
    m = re.search(r'PAYMENT\s+DUE\s+DATE\s*\n\s*([A-Za-z0-9 ,\/\-]+)', text, re.I)
    if m:
        due_date = m.group(1).strip()
    else:
        m = re.search(r'Payment\s+Due\s+Date\s*[:\-]\s*([A-Za-z0-9,\/\- ]+)', text, re.I)
        if m:
            due_date = m.group(1).strip()

    # 5) Total amount due & minimum amount due
    total_due = None
    minimum_due = None
    m = re.search(r'Total\s+Amount\s+due\s*\n\s*[`₹Rs\$\s]*([0-9,]+\.\d{2})', text, re.I)
    if m:
        total_due = _clean_amount(m.group(1))
    else:
        m = re.search(r'Total\s+Amount\s+due[:\s\-]*[`₹Rs\$\s]*([0-9,]+\.\d{2})', text, re.I)
        if m:
            total_due = _clean_amount(m.group(1))

    m2 = re.search(r'Minimum\s+Amount\s+due\s*\n\s*[`₹Rs\$\s]*([0-9,]+\.\d{2})', text, re.I)
    if m2:
        minimum_due = _clean_amount(m2.group(1))
    else:
        m2 = re.search(r'Minimum\s+Amount\s+due[:\s\-]*[`₹Rs\$\s]*([0-9,]+\.\d{2})', text, re.I)
        if m2:
            minimum_due = _clean_amount(m2.group(1))

    
    transactions = []
    lines = text_norm.splitlines()
    date_re = re.compile(r'^\s*(\d{2}/\d{2}/\d{4})')  
    amount_re = re.compile(r'([0-9,]+\.\d{2})(?:\s*CR)?\s*$') 
    for ln in lines:
        ln_strip = ln.strip()
        if not ln_strip:
            continue
        mdate = date_re.match(ln_strip)
        if mdate:
            tdate = mdate.group(1)
            mamt = amount_re.search(ln_strip)
            if not mamt:
                mamt = re.search(r'([0-9,]+\.\d{2})', ln_strip[::-1])
                
                if not mamt:
                   
                    continue
         
            mamt = amount_re.search(ln_strip)
            if mamt:
                raw_amount = mamt.group(1)
                credit_flag = bool(re.search(r'\bCR\b', ln_strip))
                amt = _clean_amount(raw_amount)
                # description: remove date at start, remove possible serial number token immediately after date, and remove trailing amount
                desc = ln_strip
                # remove date
                desc = re.sub(r'^\s*\d{2}/\d{2}/\d{4}\s*', '', desc)
                # remove trailing amount and optional CR
                desc = re.sub(r'([0-9,]+\.\d{2})(?:\s*CR)?\s*$', '', desc).strip()
                # remove serial token at start if all digits (e.g. 4566690290)
                desc = re.sub(r'^\d+\s*', '', desc)
                # normalize multiple spaces
                desc = re.sub(r'\s{2,}', ' ', desc).strip()
                transactions.append({
                    "date": tdate,
                    "description": desc,
                    "amount": amt,
                    "credit": credit_flag
                })
            else:
                # fallback: try to extract any amount inside line
                m_any = re.search(r'([0-9,]+\.\d{2})', ln_strip)
                if m_any:
                    amt = _clean_amount(m_any.group(1))
                    desc = re.sub(r'^\s*\d{2}/\d{2}/\d{4}\s*', '', ln_strip)
                    desc = re.sub(r'^\d+\s*', '', desc)
                    transactions.append({
                        "date": tdate,
                        "description": desc.strip(),
                        "amount": amt,
                        "credit": bool(re.search(r'\bCR\b', ln_strip))
                    })

    # Keep reasonable cap (avoid huge lists)
    if len(transactions) > 1000:
        transactions = transactions[:1000]

    return {
        "bank": "ICICI Bank",
        "card_last4": card_last4,
        "statement_date": statement_date,
        "statement_period": statement_period,
        "due_date": due_date,
        "total_due": total_due,
        "minimum_due": minimum_due,
        "transactions": transactions
    }
