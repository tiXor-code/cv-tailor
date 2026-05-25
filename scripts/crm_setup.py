#!/usr/bin/env python3
"""One-time setup of the Pipeline tab: headers, validation, formatting.

Idempotent: re-running re-applies the same formatting without duplicating rows.

Usage:
  python scripts/crm_setup.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.sheets import HEADERS, STATUSES, get_pipeline_worksheet

# Background colors per status — RGB floats 0..1 for gspread's format API.
STATUS_COLORS = {
    "Saved":     {"red": 0.94, "green": 0.94, "blue": 0.94},
    "Applied":   {"red": 0.81, "green": 0.89, "blue": 1.00},
    "Reply":     {"red": 1.00, "green": 0.95, "blue": 0.80},
    "Interview": {"red": 1.00, "green": 0.88, "blue": 0.70},
    "Offer":     {"red": 0.78, "green": 0.90, "blue": 0.79},
    "Rejected":  {"red": 0.96, "green": 0.77, "blue": 0.79},
    "Ghosted":   {"red": 0.96, "green": 0.77, "blue": 0.79},
}


def main():
    ws = get_pipeline_worksheet()

    # 1. Headers.
    ws.update("A1", [HEADERS])

    # 2. Bold header row + light grey background.
    ws.format("A1:I1", {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
    })

    # 3. Freeze the header row.
    ws.freeze(rows=1)

    # 4. Auto-filter on row 1.
    ws.set_basic_filter("A1:I1")

    # 5/6/7. Data validation, conditional formatting, column widths via batch_update.
    spreadsheet = ws.spreadsheet
    requests = [
        {
            "setDataValidation": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": 1,
                    "startColumnIndex": 6,
                    "endColumnIndex": 7,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": s} for s in STATUSES],
                    },
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        }
    ]

    for status, color in STATUS_COLORS.items():
        text_format = {}
        if status == "Rejected":
            text_format = {"italic": True}
        elif status == "Ghosted":
            text_format = {"strikethrough": True}
        rule = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": ws.id,
                        "startRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 9,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f'=$G2="{status}"'}],
                        },
                        "format": {
                            "backgroundColor": color,
                            **({"textFormat": text_format} if text_format else {}),
                        },
                    },
                },
                "index": 0,
            }
        }
        requests.append(rule)

    widths_px = [120, 200, 120, 250, 320, 100, 90, 160, 320]
    for i, w in enumerate(widths_px):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": i,
                    "endIndex": i + 1,
                },
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })

    spreadsheet.batch_update({"requests": requests})

    print(f"Pipeline tab configured: {len(HEADERS)} columns, {len(STATUSES)} statuses.")
    print(f"Spreadsheet URL: {spreadsheet.url}")


if __name__ == "__main__":
    main()
