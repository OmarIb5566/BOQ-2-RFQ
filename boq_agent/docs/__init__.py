"""Specifications & drawings: optional uploads matched to the work packages.

    sort.py      what each uploaded file is (BOQ / spec / drawing), without reading it in full
    specs.py     a spec PDF -> its sections (by the document's own SECTION numbering)
    drawings.py  a drawing PDF / image -> one record per sheet (number, title, trade, legend)
    vision.py    the vision model, only for pages without a text layer (scans, images)
    match.py     sections & sheets -> work packages (codes, keywords, sheet type; LLM only for leftovers)
    export.py    one Specs PDF and one Drawings PDF per work package (whole pages / whole sheets)
"""
