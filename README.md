# 📈 Tradetron PNL Auto-Updater

Auto-fetches strategy PNL from Tradetron every trading day at **3:16 PM IST**, saves to Google Drive, and displays via a Streamlit dashboard.

---

## 🗂️ Project Structure

```
.github/
  workflows/
    daily_pnl_update.yml          ← GitHub Actions workflow (runs Mon–Fri 3:16 PM IST)

scripts/
  tradetron_auth.py               ← Step 1: Login to Tradetron, save session
  tradetron_scraper.py            ← Step 2: Scrape strategies + PNL → CSV files
  google_drive_uploader.py        ← Step 3: Upload CSVs to Google Drive

dashboard.py                      ← Streamlit dashboard (deploy on share.streamlit.io)
requirements.txt
README.md
```

---

## 📁 File Flow

```
GitHub Actions Runner (temp disk per run)
│
├── tradetron_auth.py
│     writes → tradetron_session.json
│
├── tradetron_scraper.py
│     reads  → tradetron_session.json
│     writes → pnl_2024-05-01_15-16.csv   (timestamped snapshot)
│              pnl_latest.csv              (always overwritten)
│              snapshot_path.txt           (filename pointer for uploader)
│
└── google_drive_uploader.py
      reads  → snapshot_path.txt
               pnl_2024-05-01_15-16.csv
               pnl_latest.csv
      uploads→ Google Drive folder
                 ├── pnl_2024-05-01_15-16.csv  (new file, keeps history)
                 └── pnl_latest.csv             (overwritten each run)
```

---

## 🔧 Setup

### 1. Create GitHub Repository
```bash
git init tradetron-pnl
cd tradetron-pnl
git add .
git commit -m "Initial setup"
git remote add origin https://github.com/YOUR_USERNAME/tradetron-pnl.git
git push -u origin main
```

### 2. Add GitHub Secrets
Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name | Value |
|---|---|
| `TRADETRON_EMAIL` | Your Tradetron login email |
| `TRADETRON_PASSWORD` | Your Tradetron password |
| `GOOGLE_DRIVE_FOLDER_ID` | Folder ID from your Google Drive URL |
| `GOOGLE_CREDENTIALS_JSON` | Full JSON content of your service account key |

### 3. Google Drive Setup
1. [Google Cloud Console](https://console.cloud.google.com) → New project → Enable **Google Drive API**
2. Create a **Service Account** → Download JSON key
3. Paste full JSON as `GOOGLE_CREDENTIALS_JSON` secret
4. Create a Google Drive folder → copy the ID from the URL
5. **Share that folder** with the `client_email` from the service account JSON

### 4. Deploy Streamlit Dashboard
1. Push repo to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select `dashboard.py`
3. Under **Advanced settings → Secrets**, add:
```toml
GOOGLE_DRIVE_FOLDER_ID = "your_folder_id_here"
GOOGLE_CREDENTIALS_JSON = '''
{ ...full service account JSON here... }
'''
```

---

## ⏰ Schedule
Runs at **3:16 PM IST (9:46 AM UTC)** Mon–Fri.
Manual trigger: **Actions tab → Daily Tradetron PNL Update → Run workflow**