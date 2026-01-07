import pymupdf  # PyMuPDF

doc = pymupdf.open("junkyard/Example_G-28.pdf")
for page in doc:
    for w in (page.widgets() or []):
        print(
            w.field_name,
            w.field_type_string,
            w.field_value,
            w.field_label,
            w.choice_values
        )
