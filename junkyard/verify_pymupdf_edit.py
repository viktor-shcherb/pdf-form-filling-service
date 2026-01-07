import pymupdf

doc = pymupdf.open("junkyard/Example_G-28.pdf")

values = {
    "GivenName": "Ada",
    "FamilyName": "Lovelace",
}

for page in doc:
    for w in (page.widgets() or []):
        for key in values:
            if key in w.field_name:
                w.field_value = values[key]
                w.update()
                break

        # focus on text fields only for now
        if "_State" in w.field_name:
            w.field_value = w.choice_values[0]

doc.save("junkyard/Example_G-28_filled.pdf")
doc.close()
