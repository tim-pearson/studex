#!/usr/bin/env bash
# Run Marker on PDF(s) and convert the produced JSON into clean Markdown.
#
# This is a turnkey version of the manual workflow:
#
#   SURYA_INFERENCE_BACKEND=llamacpp marker_single test.pdf \
#     --mode balanced --ocr_inline_math true --ocr_equations true \
#     --output_format json --output_dir marker-test/json
#   python3 json_to_md.py marker-test/json/.../test.json -o .../test.md
#
# Usage:
#   ./marker_to_md.sh test.pdf                       # -> marker-test/json/<stem>/<stem>.md
#   ./marker_to_md.sh test.pdf -o out
#   ./marker_to_md.sh *.pdf
#
# Configuration (all optional, override via environment):
#   MARKER_BIN          marker_single binary (default: marker_single on PATH)
#   MARKER_MODE         balanced / accurate / fast  (default: balanced)
#   OCR_INLINE_MATH     true|false                  (default: true)
#   OCR_EQUATIONS       true|false                  (default: true)
#   OUTPUT_DIR          base output folder          (default: marker-test/json)
#   CONVERTER           path to json_to_md.py       (default: next to this script)
#   PYTHON              python interpreter          (default: python3)
set -euo pipefail

MARKER_BIN="${MARKER_BIN:-marker_single}"
MODE="${MARKER_MODE:-balanced}"
OCR_INLINE_MATH="${OCR_INLINE_MATH:-true}"
OCR_EQUATIONS="${OCR_EQUATIONS:-true}"
OUTPUT_DIR="${OUTPUT_DIR:-marker-test/json}"
PYTHON="${PYTHON:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONVERTER="${CONVERTER:-$SCRIPT_DIR/json_to_md.py}"

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

# Parse options before positional PDFs
OUT=""
PDFS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output-dir) OUT="$2"; shift 2 ;;
        -h|--help) usage ;;
        -*) echo "Unknown option: $1" >&2; usage ;;
        *) PDFS+=("$1"); shift ;;
    esac
done
[ "${#PDFS[@]}" -eq 0 ] && usage
[ -n "$OUT" ] && OUTPUT_DIR="$OUT"

if ! command -v "$MARKER_BIN" >/dev/null 2>&1 && [ ! -x "$MARKER_BIN" ]; then
    echo "error: '$MARKER_BIN' not found (install marker with: pip install marker-pdf)" >&2
    exit 1
fi
[ -f "$CONVERTER" ] || { echo "error: converter not found: $CONVERTER" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR"

for pdf in "${PDFS[@]}"; do
    [ -f "$pdf" ] || { echo "error: no such file: $pdf" >&2; continue; }
    stem="$(basename "$pdf")"
    stem="${stem%.pdf}"

    echo "==> marker_single: $pdf"
    SURYA_INFERENCE_BACKEND=llamacpp "$MARKER_BIN" "$pdf" \
        --mode "$MODE" \
        --ocr_inline_math "$OCR_INLINE_MATH" \
        --ocr_equations "$OCR_EQUATIONS" \
        --output_format json \
        --output_dir "$OUTPUT_DIR"

    # Locate the JSON that marker produced for this PDF
    json="$(find "$OUTPUT_DIR" -type f -name "${stem}.json" | head -n 1)"
    if [ -z "$json" ]; then
        json="$(find "$OUTPUT_DIR" -type f -name "*.json" ! -name "*_meta.json" | head -n 1)"
    fi
    if [ -z "$json" ]; then
        echo "error: no .json output found for '$pdf' under $OUTPUT_DIR" >&2
        continue
    fi

    echo "==> converting: $json"
    "$PYTHON" "$CONVERTER" "$json" -o "$OUTPUT_DIR/$stem/$stem.md"
done

echo "Done. Markdown files are under: $OUTPUT_DIR"