import fitz  # PyMuPDF

def extract_text_pymupdf(file_obj):
    """
    Extracts all text from a PDF file using PyMuPDF.
    Works with both file paths and file-like objects.
    """
    text = ""
    # Handle both path and uploaded file object
    if hasattr(file_obj, "read"):
        pdf = fitz.open(stream=file_obj.read(), filetype="pdf")
    else:
        pdf = fitz.open(file_obj)

    for page in pdf:
        text += page.get_text("text")

    pdf.close()
    return text
