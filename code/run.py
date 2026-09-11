#!/usr/bin/env python3
"""Entry point: score the fetched raw data and write every output file."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor, make_pdf, make_doc
if __name__ == "__main__":
    monitor.run_all(pdf_builder=make_pdf.build, doc_builder=make_doc)
