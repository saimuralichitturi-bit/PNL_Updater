
"""
onedrive_excel_updater.py
─────────────────────────
Updates OneDrive Excel file with Tradetron PNL data (EOD only).
Uses refresh token for automatic authentication — NO manual login needed.

Excel Column Mapping (A to Q):
  A  = Date
  B  = DI3STS918 PDF   → DI3STS N 918 PDF                          (PNL/Capital = ROI)
  C  = DI3STS PDF TBS  → avg(DI3STS N 1015 PDF, DI3STS N 1115 PDF) (avg of individual ROIs)
  D  = DI3STB918 PDF   → DI3STB 918 PDF                            (PNL/Capital = ROI)
  E  = V3V2 S TBS      → avg(V3v2 S 1020, V3v2 S 1120)             (avg of individual ROIs)
  F  = V3V2 S          → V3v2 S                                     (PNL/Capital = ROI)
  G  = IDNO TBS        → avg(IDNO10, IDNO10 1020, IDNO10 1120)      (avg of individual ROIs)
  H  = IDNO            → sum(IDNO10, IDNO10 1020, IDNO10 1120)      (sumPNL / sumCap = Combined ROI)
  I  = DI3STB          → DI3STB V1                                  (PNL/Capital = ROI)
  J  = V3v2 N          → V3v2 N                                     (PNL/Capital = ROI)
  K  = V3v2 N tbs      → avg(V3v2 N 1020, V3v2 N 1120, V3v2 N SF)  (avg of individual ROIs)
  L  = NDACS           → NDACS ATM ID                               (PNL/Capital = ROI)
  M  = NDATC           → NDATC ATM ID                               (PNL/Capital = ROI)
  N  = DI3STS          → DI3STS ATM ID                              (PNL/Capital = ROI)
  O  = IDSO            → IDSO                                       (PNL/Capital = ROI)
  P  = STOSS           → sum(STOSS N 5M, STOSS ND)                  (sumPNL / sumCap = Combined ROI)
  Q  = IDSO TBS        → avg(IDSO10 1020, IDSO10 1120)              (avg of individual ROIs)

Calculation methods:
  ROI          = PNL (Overall) / Capital                  (single strategy)
  Average ROI  = mean of individual strategy ROIs         (TBS columns: C, E, G, K, Q)
  Combined ROI = sum(PNL) / sum(Capital) across group     (pooled columns: H, P)

Reads:  pnl_latest.csv (written by tradetron_scraper.py)
Writes: Appends/updates today's row in OneDrive Excel file
"""

import os
import csv
import sys
import requests
from datetime import datetime
import pytz

# ── Configuration ──────────────────────────────────────────────────────────────
CLIENT_ID     = os.environ.get("MICROSOFT_CLIENT_ID", os.environ.get("AZURE_CLIENT_ID", "8c359df1-2487-4327-a61d-7a80ad091925"))
CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", os.environ.get("AZURE_CLIENT_SECRET"))
REFRESH_TOKEN = os.environ.get("AZURE_REFRESH_TOKEN")
TENANT_ID     = "common"

DRIVE_ID      = "33c10b089d10f9f8"
ITEM_ID       = "33C10B089D10F9F8!s24680b3f76114bb08ce3914343d00262"
SHEET_NAME    = "Sheet1"
PNL_CSV       = "pnl_latest.csv"
IST           = pytz.timezone("Asia/Kolkata")

# ── Calculation method constants ───────────────────────────────────────────────
ROI      = "roi"        # single strategy: PNL / Capital
AVG_ROI  = "avg_roi"    # average of individual strategy ROIs (TBS columns)
COMB_ROI = "comb_roi"   # pooled: sumPNL / sumCapital (IDNO group, STOSS group)

# ── Strategy → Excel column mapping ───────────────────────────────────────────
# Each entry: "COLUMN_HEADER": {"strategies": [...], "method": ROI|AVG_ROI|COMB_ROI}
COLUMN_MAP = {
    "DI3STS918 PDF":  {"strategies": ["DI3STS N 918 PDF"],                                        "method": ROI},
    "DI3STS PDF TBS": {"strategies": ["DI3STS N 1015 PDF", "DI3STS N 1115 PDF"],                  "method": AVG_ROI},
    "DI3STB918 PDF":  {"strategies": ["DI3STB 918 PDF"],                                          "method": ROI},
    "V3V2 S TBS":     {"strategies": ["V3v2 S 1020", "V3v2 S 1120"],                              "method": AVG_ROI},
    "V3V2 S":         {"strategies": ["V3v2 S"],                                                   "method": ROI},
    "IDNO TBS":       {"strategies": ["IDNO10", "IDNO10 1020", "IDNO10 1120"],                    "method": AVG_ROI},
    "IDNO":           {"strategies": ["IDNO10", "IDNO10 1020", "IDNO10 1120"],                    "method": COMB_ROI},
    "DI3STB":         {"strategies": ["DI3STB V1"],                                               "method": ROI},
    "V3v2 N":         {"strategies": ["V3v2 N"],                                                   "method": ROI},
    "V3v2 N tbs":     {"strategies": ["V3v2 N 1020", "V3v2 N 1120", "V3v2 N SF"],                "method": AVG_ROI},
    "NDACS":          {"strategies": ["NDACS ATM ID"],                                             "method": ROI},
    "NDATC":          {"strategies": ["NDATC ATM ID"],                                             "method": ROI},
    "DI3STS":         {"strategies": ["DI3STS ATM ID"],                                           "method": ROI},
    "IDSO":           {"strategies": ["IDSO"],                                                     "method": ROI},
    "STOSS":          {"strategies": ["STOSS N 5M", "STOSS ND"],                                  "method": COMB_ROI},
    "IDSO TBS":       {"strategies": ["IDSO10 1020", "IDSO10 1120"],                              "method": AVG_ROI},
}

# Column order matching Excel A→Q (A=Date, then B→Q)
COLUMN_ORDER = [
    "DI3STS918 PDF",
    "DI3STS PDF TBS",
    "DI3STB918 PDF",
    "V3V2 S TBS",
    "V3V2 S",
    "IDNO TBS",
    "IDNO",
    "DI3STB",
    "V3v2 N",
    "V3v2 N tbs",
    "NDACS",
    "NDATC",
    "DI3STS",
    "IDSO",
    "STOSS",
    "IDSO TBS",
]


# ── Authentication ─────────────────────────────────────────────────────────────
def get_access_token() -> str:
    print("[OneDrive] Getting access token from refresh token...")

    resp = requests.post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type":    "refresh_token",
            "scope":         "https://graph.microsoft.com/Files.ReadWrite offline_access",
        },
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"[OneDrive] ✗ Token refresh failed: {resp.status_code}")
        print(resp.json())
        sys.exit(1)

    tokens = resp.json()
    new_refresh = tokens.get("refresh_token")
    if new_refresh and new_refresh != REFRESH_TOKEN:
        print("[OneDrive] ⚠️  New refresh token issued!")
        print(f"[OneDrive]    Update AZURE_REFRESH_TOKEN secret with: {new_refresh[:30]}...")

    print("[OneDrive] ✓ Access token obtained")
    return tokens["access_token"]


# ── Read PNL CSV ───────────────────────────────────────────────────────────────
def read_pnl_csv() -> dict:
    """
    Returns dict: { strategy_name: {"pnl": float, "capital": float} }
    Uses 'Capital' column (HTML-scraped), falls back to 'Capital Required'.
    """
    if not os.path.exists(PNL_CSV):
        print(f"[OneDrive] ✗ {PNL_CSV} not found")
        sys.exit(1)

    data = {}
    with open(PNL_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("Strategy Name", "").strip()
            if not name:
                continue

            pnl = float(row.get("PNL (Overall)", 0) or 0)

            # Try HTML capital first, then API capital
            capital = float(row.get("Capital", 0) or 0)
            if capital == 0:
                capital = float(row.get("Capital (HTML)", 0) or 0)
            if capital == 0:
                capital = float(row.get("Capital Required", 0) or 0)

            data[name] = {"pnl": pnl, "capital": capital}

    print(f"[OneDrive] ✓ Loaded {len(data)} strategies from CSV")
    for name, vals in data.items():
        print(f"  {name:<35} PNL: ₹{vals['pnl']:>12,.2f}  Capital: ₹{vals['capital']:>12,.0f}")

    return data


# ── Compute column values ──────────────────────────────────────────────────────
def compute_column_values(strategy_data: dict) -> dict:
    """
    Compute ROI for each Excel column using the correct formula method:

      ROI       (single)  : PNL / Capital  × 100
      AVG_ROI   (TBS)     : mean of each strategy's individual ROI  × 100
      COMB_ROI  (pooled)  : sumPNL / sumCapital  × 100

    Returns dict: { column_name: roi_value_percent }
    """
    results = {}

    for col_name, cfg in COLUMN_MAP.items():
        strategy_names = cfg["strategies"]
        method         = cfg["method"]

        found   = []
        missing = []
        for sname in strategy_names:
            if sname in strategy_data:
                found.append(sname)
            else:
                missing.append(sname)

        if missing:
            print(f"[OneDrive] ⚠️  Column '{col_name}' — missing strategies: {missing}")

        if not found:
            results[col_name] = 0.0
            print(f"  {col_name:<16} NO DATA  ROI=0.0%")
            continue

        # ── ROI: single strategy (or first-found if only one expected) ─────────
        if method == ROI:
            s = found[0]
            pnl = strategy_data[s]["pnl"]
            cap = strategy_data[s]["capital"]
            roi = round((pnl / cap) * 100, 1) if cap > 0 else 0.0
            print(f"  {col_name:<16} [{s}] PNL=₹{pnl:>10,.2f}  Cap=₹{cap:>10,.0f}  ROI={roi:.1f}%")

        # ── AVG_ROI: average of each strategy's individual ROI ────────────────
        elif method == AVG_ROI:
            individual_rois = []
            for s in found:
                pnl = strategy_data[s]["pnl"]
                cap = strategy_data[s]["capital"]
                if cap > 0:
                    individual_rois.append((pnl / cap) * 100)
                else:
                    print(f"[OneDrive] ⚠️  '{s}' has capital=0, excluded from avg")
            roi = round(sum(individual_rois) / len(individual_rois), 1) if individual_rois else 0.0
            roi_parts = ", ".join(f"{r:.1f}%" for r in individual_rois)
            print(f"  {col_name:<16} avg({roi_parts}) = {roi:.1f}%")

        # ── COMB_ROI: pool all PNL and capital, then divide ───────────────────
        elif method == COMB_ROI:
            total_pnl = sum(strategy_data[s]["pnl"]     for s in found)
            total_cap = sum(strategy_data[s]["capital"]  for s in found)
            roi = round((total_pnl / total_cap) * 100, 1) if total_cap > 0 else 0.0
            print(f"  {col_name:<16} sumPNL=₹{total_pnl:>10,.2f}  sumCap=₹{total_cap:>10,.0f}  ROI={roi:.1f}%")

        else:
            roi = 0.0

        results[col_name] = roi

    return results


# ── Find today's row or append new row ────────────────────────────────────────
def find_or_create_row(headers: dict, today_str: str) -> int:
    """
    Search column A for today's date.
    Returns row number (1-based). If not found, returns next empty row.
    """
    url = (
        f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/items/{ITEM_ID}"
        f"/workbook/worksheets/{SHEET_NAME}/usedRange"
    )
    resp = requests.get(url, headers=headers)

    if resp.status_code != 200:
        print(f"[OneDrive] ✗ Could not get used range: {resp.status_code}")
        return 2  # Default to row 2

    used = resp.json()
    values = used.get("values", [])
    row_count = len(values)

    # Search column A (index 0) for today's date
    for i, row in enumerate(values):
        if row and str(row[0]).strip() == today_str:
            print(f"[OneDrive] ✓ Found today's date in row {i + 1} — will update")
            return i + 1

    # Not found — append after last row
    next_row = row_count + 1
    print(f"[OneDrive] Today's date not found — appending to row {next_row}")
    return next_row


# ── Write row to Excel ─────────────────────────────────────────────────────────
def write_excel_row(headers: dict, row_number: int, today_str: str, col_values: dict):
    """Write one row: A=date, B-Q=ROI values"""

    # Build values list: [date, col_B, col_C, ..., col_Q]
    row_data = [today_str]
    for col_name in COLUMN_ORDER:
        row_data.append(col_values.get(col_name, 0.0))

    # Range: A{row}:Q{row} (17 columns: A + 16 data columns)
    end_col    = chr(ord("A") + len(row_data) - 1)  # "Q"
    range_addr = f"A{row_number}:{end_col}{row_number}"

    url = (
        f"https://graph.microsoft.com/v1.0/drives/{DRIVE_ID}/items/{ITEM_ID}"
        f"/workbook/worksheets/{SHEET_NAME}/range(address='{range_addr}')"
    )

    resp = requests.patch(url, headers=headers, json={"values": [row_data]})

    if resp.status_code == 200:
        print(f"[OneDrive] ✓ Row {row_number} written — range {range_addr}")
        print(f"[OneDrive]   Date: {today_str}")
        for i, col_name in enumerate(COLUMN_ORDER):
            val    = col_values.get(col_name, 0.0)
            method = COLUMN_MAP[col_name]["method"]
            print(f"  {col_name:<16} = {val:.1f}%  [{method}]")
    else:
        print(f"[OneDrive] ✗ Write failed: {resp.status_code}")
        print(resp.json())
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "=" * 70)
    print("  OneDrive Excel Updater — EOD Mode")
    print("=" * 70 + "\n")

    # Validate secrets
    if not CLIENT_SECRET:
        print("[OneDrive] ✗ MICROSOFT_CLIENT_SECRET not set in GitHub Secrets")
        sys.exit(1)
    if not REFRESH_TOKEN:
        print("[OneDrive] ✗ AZURE_REFRESH_TOKEN not set in GitHub Secrets")
        sys.exit(1)

    # Step 1: Auth
    access_token = get_access_token()
    graph_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }

    # Step 2: Read CSV
    print("\n[OneDrive] Reading PNL data...")
    strategy_data = read_pnl_csv()

    # Step 3: Compute ROI values
    print("\n[OneDrive] Computing column values...")
    col_values = compute_column_values(strategy_data)

    # Step 4: Get today's date (IST)
    now_ist   = datetime.now(IST)
    today_str = now_ist.strftime("%m/%d/%Y")  # Excel date format
    print(f"\n[OneDrive] Today (IST): {today_str}")

    # Step 5: Find/create row
    row_number = find_or_create_row(graph_headers, today_str)

    # Step 6: Write row
    print(f"\n[OneDrive] Writing to row {row_number}...")
    write_excel_row(graph_headers, row_number, today_str, col_values)

    print("\n" + "=" * 70)
    print("  ✓ Excel file updated successfully!")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()