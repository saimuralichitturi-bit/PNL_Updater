"""
dashboard.py
────────────
Streamlit dashboard to view and download Tradetron PNL snapshots from Google Drive.

Streamlit Secrets required:
  GOOGLE_DRIVE_FOLDER_ID   = "your_folder_id"
  GOOGLE_CREDENTIALS_JSON  = '{ ...service account JSON... }'
"""

import streamlit as st
import pandas as pd
import json
import io
from datetime import datetime
import pytz

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Tradetron PNL Dashboard",
    page_icon="📈",
    layout="wide",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap');
    html, body, [class*="css"] { font-family: 'Syne', sans-serif; }
    code, .stDataFrame { font-family: 'JetBrains Mono', monospace; }
    .stApp { background: #0a0d14; color: #e2e8f0; }
    .metric-card {
        background: linear-gradient(135deg, #111827 0%, #1f2937 100%);
        border: 1px solid #374151; border-radius: 12px;
        padding: 20px; text-align: center;
    }
    .metric-label { color: #9ca3af; font-size: 12px; text-transform: uppercase; letter-spacing: 2px; }
    .metric-value { font-size: 32px; font-weight: 800; margin-top: 4px; }
    .positive { color: #10b981; }
    .negative { color: #ef4444; }
    .neutral  { color: #e2e8f0; }
    .file-card {
        background: #111827; border: 1px solid #1f2937; border-radius: 8px;
        padding: 12px 16px; margin: 6px 0;
    }
    .timestamp-badge {
        background: #1e3a5f; color: #60a5fa;
        padding: 3px 10px; border-radius: 999px;
        font-size: 12px; font-family: 'JetBrains Mono', monospace;
    }
    h1, h2, h3 { font-family: 'Syne', sans-serif; font-weight: 800; }
</style>
""", unsafe_allow_html=True)

# ── Google Drive auth ──────────────────────────────────────────────────────────
@st.cache_resource
def get_drive_service():
    creds_raw  = st.secrets["GOOGLE_CREDENTIALS_JSON"]
    creds_dict = json.loads(creds_raw) if isinstance(creds_raw, str) else creds_raw
    creds      = Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)

# ── Drive helpers ──────────────────────────────────────────────────────────────
def list_csv_files(drive, folder_id):
    results = drive.files().list(
        q=f"'{folder_id}' in parents and name contains '.csv' and trashed=false",
        orderBy="createdTime desc",
        fields="files(id, name, createdTime, size)",
        pageSize=50
    ).execute()
    return results.get("files", [])

def download_csv(drive, file_id) -> pd.DataFrame:
    request    = drive.files().get_media(fileId=file_id)
    buffer     = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return pd.read_csv(buffer)

def format_filename_as_datetime(filename: str) -> str:
    """
    Convert pnl_2024-05-01_15-16.csv → '01 May 2024, 03:16 PM IST'
    Falls back gracefully if format doesn't match.
    """
    try:
        base   = filename.replace("pnl_", "").replace(".csv", "")  # 2024-05-01_15-16
        dt     = datetime.strptime(base, "%Y-%m-%d_%H-%M")
        ist    = pytz.timezone("Asia/Kolkata")
        dt_ist = pytz.utc.localize(dt).astimezone(ist)
        return dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return filename   # return raw name if parsing fails

# ── UI ─────────────────────────────────────────────────────────────────────────
st.markdown("# 📈 Tradetron PNL Dashboard")
st.markdown("---")

try:
    drive     = get_drive_service()
    folder_id = st.secrets["GOOGLE_DRIVE_FOLDER_ID"]
except Exception as e:
    st.error(f"❌ Could not connect to Google Drive. Check your Streamlit secrets.\n\n`{e}`")
    st.info("Required secrets: `GOOGLE_CREDENTIALS_JSON` and `GOOGLE_DRIVE_FOLDER_ID`")
    st.stop()

# ── Fetch file list ────────────────────────────────────────────────────────────
with st.spinner("Fetching snapshots from Google Drive..."):
    all_files = list_csv_files(drive, folder_id)

if not all_files:
    st.warning("No CSV files found in the Drive folder. Run the GitHub Action first.")
    st.stop()

latest_file   = next((f for f in all_files if f["name"] == "pnl_latest.csv"), None)
snapshot_files = [f for f in all_files if f["name"] != "pnl_latest.csv"]

# ── Sidebar file selector ──────────────────────────────────────────────────────
st.sidebar.markdown("## 📂 Select Snapshot")

options = {}
if latest_file:
    options["⚡ Latest Snapshot"] = latest_file
for f in snapshot_files:
    label = format_filename_as_datetime(f["name"])
    options[label] = f

selected_label = st.sidebar.selectbox("Choose a snapshot:", list(options.keys()))
selected_file  = options[selected_label]

# ── Load the CSV ───────────────────────────────────────────────────────────────
with st.spinner(f"Loading {selected_file['name']}..."):
    df = download_csv(drive, selected_file["id"])

# ── Snapshot timestamp row ─────────────────────────────────────────────────────
snapshot_time = ""
if "Snapshot Time" in df.columns:
    snapshot_time = df["Snapshot Time"].iloc[0]            # from inside CSV
elif selected_file["name"] != "pnl_latest.csv":
    snapshot_time = format_filename_as_datetime(selected_file["name"])   # from filename

col_ts, col_btn = st.columns([4, 1])
with col_ts:
    if snapshot_time:
        st.markdown(
            f"<span class='timestamp-badge'>🕐 Snapshot recorded: {snapshot_time}</span>",
            unsafe_allow_html=True
        )
with col_btn:
    if st.button("🔄 Refresh"):
        st.cache_resource.clear()
        st.rerun()

st.markdown("<br>", unsafe_allow_html=True)

# ── Summary metric cards ───────────────────────────────────────────────────────
if "PNL (Today)" in df.columns:
    total_today   = pd.to_numeric(df["PNL (Today)"],   errors="coerce").sum()
    total_overall = pd.to_numeric(df["PNL (Overall)"], errors="coerce").sum()
    total_capital = pd.to_numeric(df.get("Capital", pd.Series([0])), errors="coerce").sum()
    active_count  = (df.get("Status", pd.Series([])).str.lower() == "running").sum()

    def clr(val): return "positive" if val >= 0 else "negative"
    def arrow(val): return "▲" if val >= 0 else "▼"

    m1, m2, m3, m4 = st.columns(4)
    for col, label, val, fmt in [
        (m1, "Today's PNL",     total_today,   f"{arrow(total_today)} ₹{abs(total_today):,.0f}"),
        (m2, "Overall PNL",     total_overall, f"{arrow(total_overall)} ₹{abs(total_overall):,.0f}"),
        (m3, "Total Capital",   total_capital, f"₹{total_capital:,.0f}"),
        (m4, "Active Strategies", active_count, f"{active_count} / {len(df)}"),
    ]:
        css = clr(val) if label in ("Today's PNL", "Overall PNL") else "neutral"
        col.markdown(f"""
        <div class="metric-card">
            <div class="metric-label">{label}</div>
            <div class="metric-value {css}">{fmt}</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

# ── Data table ─────────────────────────────────────────────────────────────────
st.markdown("### 📊 Strategy-wise PNL")

display_df = df.drop(columns=["Snapshot Time"], errors="ignore")

pnl_cols = [c for c in display_df.columns if "pnl" in c.lower()]
def style_pnl(val):
    try:
        v = float(val)
        return f"color: {'#10b981' if v >= 0 else '#ef4444'}; font-weight: 700"
    except Exception:
        return ""

styled = display_df.style.applymap(style_pnl, subset=pnl_cols) if pnl_cols else display_df.style
st.dataframe(styled, use_container_width=True, height=420)

# ── Download button ────────────────────────────────────────────────────────────
st.markdown("### ⬇️ Download")
csv_bytes     = df.to_csv(index=False).encode("utf-8")
download_name = selected_file["name"] if selected_file["name"].endswith(".csv") else "pnl_export.csv"

col_dl, _ = st.columns([1, 3])
with col_dl:
    st.download_button(
        label="📥 Download this CSV",
        data=csv_bytes,
        file_name=download_name,
        mime="text/csv",
        use_container_width=True,
    )

# ── Snapshot history ───────────────────────────────────────────────────────────
st.markdown("### 🗂️ All Snapshots in Drive")
for f in snapshot_files[:20]:
    label    = format_filename_as_datetime(f["name"])
    size_kb  = int(f.get("size", 0)) // 1024
    st.markdown(f"""
    <div class="file-card" style="display:flex;justify-content:space-between;align-items:center;">
        <span>📄 {f['name']}</span>
        <span class="timestamp-badge">{label} · {size_kb} KB</span>
    </div>""", unsafe_allow_html=True)

st.markdown(
    "<br><br><center style='color:#374151;font-size:12px;'>"
    "Auto-updated at 3:16 PM IST · Mon–Fri trading days</center>",
    unsafe_allow_html=True
)