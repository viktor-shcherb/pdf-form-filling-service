from pypdf import PdfReader

reader = PdfReader("junkyard/Example_G-28.pdf")
fields = reader.get_fields() or {}

for name, f in fields.items():
    ft = f.get("/FT")          # field type (/Tx text, /Btn button, etc.)
    val = f.get("/V")          # current value
    alt = f.get("/TU")         # tooltip/label (optional)
    print(name, "FT:", ft, "V:", val, "TU:", alt)
