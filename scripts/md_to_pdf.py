#!/usr/bin/env python3
"""
Convert ONBOARDING.md to PDF using markdown + weasyprint.
Preserves formatting, code blocks, and structure.
"""
import sys
from pathlib import Path
import markdown
from weasyprint import HTML, CSS
from datetime import datetime

PROJECT_DIR = Path(__file__).parent.parent
MD_FILE = PROJECT_DIR / "ONBOARDING.md"
PDF_FILE = PROJECT_DIR / "ONBOARDING.pdf"

# Custom CSS for professional technical documentation
CUSTOM_CSS = """
@page {
    size: letter;
    margin: 0.75in;
    @top-right {
        content: "Page " counter(page) " of " counter(pages);
        font-size: 9pt;
        color: #666;
    }
}

body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 10pt;
    line-height: 1.6;
    color: #333;
    max-width: 100%;
}

h1 {
    font-size: 24pt;
    font-weight: bold;
    color: #1a1a1a;
    border-bottom: 3px solid #0066cc;
    padding-bottom: 8pt;
    margin-top: 24pt;
    margin-bottom: 16pt;
    page-break-after: avoid;
}

h2 {
    font-size: 18pt;
    font-weight: bold;
    color: #1a1a1a;
    border-bottom: 2px solid #0066cc;
    padding-bottom: 6pt;
    margin-top: 20pt;
    margin-bottom: 12pt;
    page-break-after: avoid;
}

h3 {
    font-size: 14pt;
    font-weight: bold;
    color: #333;
    margin-top: 16pt;
    margin-bottom: 10pt;
    page-break-after: avoid;
}

h4 {
    font-size: 12pt;
    font-weight: bold;
    color: #444;
    margin-top: 12pt;
    margin-bottom: 8pt;
}

code {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 9pt;
    background-color: #f6f8fa;
    padding: 2pt 4pt;
    border-radius: 3pt;
    color: #d73a49;
}

pre {
    background-color: #f6f8fa;
    border: 1px solid #e1e4e8;
    border-radius: 6pt;
    padding: 12pt;
    overflow-x: auto;
    margin: 12pt 0;
    page-break-inside: avoid;
}

pre code {
    background-color: transparent;
    padding: 0;
    color: #24292e;
    font-size: 8.5pt;
    line-height: 1.45;
}

table {
    border-collapse: collapse;
    width: 100%;
    margin: 12pt 0;
    font-size: 9pt;
}

th, td {
    border: 1px solid #ddd;
    padding: 6pt 8pt;
    text-align: left;
}

th {
    background-color: #f6f8fa;
    font-weight: bold;
}

a {
    color: #0066cc;
    text-decoration: none;
}

blockquote {
    border-left: 4pt solid #0066cc;
    padding-left: 12pt;
    margin-left: 0;
    color: #666;
    font-style: italic;
}

ul, ol {
    margin: 8pt 0;
    padding-left: 24pt;
}

li {
    margin: 4pt 0;
}

hr {
    border: none;
    border-top: 1px solid #e1e4e8;
    margin: 16pt 0;
}

.toc {
    background-color: #f6f8fa;
    border: 1px solid #e1e4e8;
    padding: 12pt;
    margin-bottom: 24pt;
}
"""

def convert_md_to_pdf():
    """Convert ONBOARDING.md to PDF with proper formatting."""
    if not MD_FILE.exists():
        print(f"❌ Error: {MD_FILE} not found")
        return False

    print(f"📄 Converting {MD_FILE.name} to PDF...")
    print(f"   Source: {MD_FILE}")
    print(f"   Output: {PDF_FILE}")

    # Read markdown file
    with open(MD_FILE, 'r', encoding='utf-8') as f:
        md_content = f.read()

    # Convert markdown to HTML with extensions
    md = markdown.Markdown(extensions=[
        'tables',           # Table support
        'fenced_code',      # Code blocks with syntax highlighting
        'codehilite',       # Syntax highlighting
        'toc',              # Table of contents
        'nl2br',            # Newline to <br>
        'sane_lists',       # Better list handling
    ])

    html_body = md.convert(md_content)

    # Build complete HTML document
    html_doc = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>TUM Social AI - Sales Agent System - Complete Onboarding Guide</title>
    </head>
    <body>
        <div style="text-align: center; margin-bottom: 48pt;">
            <h1 style="border-bottom: none; font-size: 28pt; margin-bottom: 8pt;">
                TUM Social AI — Sales Agent System
            </h1>
            <h2 style="border-bottom: none; font-size: 18pt; color: #666; font-weight: normal;">
                Complete Onboarding Guide
            </h2>
            <p style="color: #888; font-size: 10pt; margin-top: 24pt;">
                Version 2.0<br>
                Last Updated: February 9, 2026<br>
                Authors: Nicolas Paul, Claude Code<br>
                Generated: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}
            </p>
        </div>
        {html_body}
        <hr style="margin-top: 48pt;">
        <p style="text-align: center; color: #888; font-size: 9pt;">
            Questions or feedback? nicolas@tum-socialaiclub.de<br>
            Built with ❤️ by TUM Social AI — https://tum-socialaiclub.de
        </p>
    </body>
    </html>
    """

    # Convert HTML to PDF
    try:
        HTML(string=html_doc).write_pdf(
            PDF_FILE,
            stylesheets=[CSS(string=CUSTOM_CSS)]
        )

        # Get file size
        file_size = PDF_FILE.stat().st_size
        size_mb = file_size / (1024 * 1024)

        print(f"✅ PDF generated successfully!")
        print(f"   File size: {size_mb:.2f} MB")
        print(f"   Location: {PDF_FILE}")
        return True

    except Exception as e:
        print(f"❌ PDF generation failed: {e}")
        return False

if __name__ == "__main__":
    success = convert_md_to_pdf()
    sys.exit(0 if success else 1)
