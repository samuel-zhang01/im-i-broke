"""Export local PDF page text for parser investigation, without ledger writes.

Usage: python -m app.extract_review statement.pdf --output review.json
       python -m app.extract_review statement.pdf --engine pymupdf4llm --output review.json

This is a review artifact, not transaction extraction or automatic ingestion.
The optional PyMuPDF4LLM engine uses local extraction and does not call an LLM.
API: https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/api.html
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pdfplumber


def extract_review(path: Path, engine: str = 'pdfplumber') -> dict:
    path = path.resolve()
    if not path.is_file() or path.suffix.lower() != '.pdf':
        raise ValueError('Provide an existing PDF file')
    if engine == 'pdfplumber':
        with pdfplumber.open(path) as pdf:
            pages = [{'page_number': i, 'text': page.extract_text(layout=True) or ''}
                     for i, page in enumerate(pdf.pages, 1)]
    elif engine == 'pymupdf4llm':
        try:
            import pymupdf4llm
        except ImportError as exc:
            raise RuntimeError('Optional engine missing; install requirements-pdf-review.txt') from exc
        chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, show_progress=False,
                                        write_images=False, embed_images=False, use_ocr=False)
        if not isinstance(chunks, list):
            raise RuntimeError('PDF extraction did not preserve page provenance')
        pages = [{'page_number': chunk['metadata']['page_number'], 'text': chunk['text']}
                 for chunk in chunks]
    else:
        raise ValueError('Unknown PDF extraction engine')
    return {
        'source_file': str(path), 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'engine': engine, 'purpose': 'local_parser_review', 'ledger_written': False,
        'page_count': len(pages), 'pages': pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--engine', choices=('pdfplumber', 'pymupdf4llm'), default='pdfplumber')
    parser.add_argument('--output', required=True, type=Path, help='Local review JSON file')
    args = parser.parse_args()
    if args.output.resolve() == args.pdf.resolve():
        parser.error('The output must be a separate JSON file')
    try:
        result = extract_review(args.pdf, args.engine)
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f'{exc}\n')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Wrote {result["page_count"]} pages to {args.output}; ledger unchanged.')


if __name__ == '__main__':
    main()
