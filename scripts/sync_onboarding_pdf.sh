#!/bin/bash
# Converts ONBOARDING.md to PDF using pandoc
# Run manually or set up as a file watcher

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MD_FILE="$PROJECT_DIR/ONBOARDING.md"
PDF_FILE="$PROJECT_DIR/ONBOARDING.pdf"

if [ ! -f "$MD_FILE" ]; then
    echo "Error: ONBOARDING.md not found at $MD_FILE"
    exit 1
fi

echo "Converting ONBOARDING.md to PDF..."
echo "Source: $MD_FILE"
echo "Output: $PDF_FILE"

# Convert with pandoc - optimized settings for technical documentation
pandoc "$MD_FILE" \
    -o "$PDF_FILE" \
    --pdf-engine=pdflatex \
    --toc \
    --toc-depth=3 \
    --number-sections \
    -V geometry:margin=1in \
    -V fontsize=11pt \
    -V linkcolor=blue \
    -V urlcolor=blue \
    -V toccolor=black \
    --highlight-style=tango \
    --metadata title="TUM Social AI - Sales Agent System" \
    --metadata subtitle="Complete Onboarding Guide" \
    --metadata author="Nicolas Paul, Claude Code" \
    --metadata date="$(date '+%B %d, %Y')" \
    2>&1

if [ $? -eq 0 ]; then
    echo "✅ PDF generated successfully: $PDF_FILE"
    echo "File size: $(du -h "$PDF_FILE" | cut -f1)"
else
    echo "❌ PDF generation failed"
    exit 1
fi
