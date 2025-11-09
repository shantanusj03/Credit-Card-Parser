import io
import streamlit as st
from parsers.pdf_utils import extract_text_pymupdf
from parsers.icici_parser import parse_icici
from parsers.hdfc_parser import parse_hdfc
from parsers.sbi_parser import parse_sbi
from parsers.kotak_parser import parse_kotak
from parsers.amex_parser import parse_amex

st.set_page_config(page_title="Credit Card PDF Parser", layout="wide")
st.title("📄 Credit Card Statement Parser")
st.write("Upload a credit card statement (ICICI, HDFC, SBI, Kotak, or Amex). The app will detect the bank automatically and extract details.")

uploaded = st.file_uploader("Upload PDF statement", type="pdf")

def detect_bank(text):
    t = text.lower()
    if "icici" in t: return "icici"
    if "hdfc" in t: return "hdfc"
    if "sbi" in t or "state bank" in t: return "sbi"
    if "kotak" in t: return "kotak"
    if "american express" in t or "amex" in t: return "amex"
    return None

if uploaded is not None:
    st.info("Processing... please wait.")
    try:
        # Read PDF
        content = uploaded.read()
        text_preview = extract_text_pymupdf(io.BytesIO(content))

        bank = detect_bank(text_preview)
        if not bank:
            st.error("Unable to detect bank automatically. Please upload a supported statement.")
        else:
            parser_input = io.BytesIO(content)

            if bank == "icici":
                data = parse_icici(parser_input)
            elif bank == "hdfc":
                data = parse_hdfc(parser_input)
            elif bank == "sbi":
                data = parse_sbi(parser_input)
            elif bank == "kotak":
                data = parse_kotak(parser_input)
            elif bank == "amex":
                data = parse_amex(parser_input)
            else:
                data = {"error": "Unsupported bank."}

            if "error" in data:
                st.error(data["error"])
            else:
                st.success(f"✅ Detected Bank: {data['bank']}")
                st.write(f"**Card Last 4:** {data.get('card_last4', 'N/A')}")
                st.write(f"**Statement Period:** {data.get('statement_date') or data.get('statement_period')}")
                st.write(f"**Payment Due Date:** {data.get('due_date')}")
                st.write(f"**Total Due:** {data.get('total_due')}")
                st.write(f"**Minimum Due:** {data.get('minimum_due')}")

                if "transactions" in data and data["transactions"]:
                    st.subheader("🧾 Transactions")
                    st.dataframe(data["transactions"])
    except Exception as e:
        st.error(f"❌ Error while parsing: {e}")
