# AI Technician Dispatch & WhatsApp Negotiation

## Architecture

```
frontend/   → Vercel (React + Vite)
backend/    → Railway (FastAPI)
database    → Supabase (PostgreSQL)
whatsapp    → Local machine only (Playwright)
```

---

## Deploy Backend → Railway

1. Push repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select this repo
4. Set environment variables in Railway dashboard:

```
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_KEY=your-service-role-key
GEMINI_API_KEY=AIzaSy...
ENABLE_WHATSAPP=false
```

Railway auto-detects `railway.json` and runs:
```
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Your backend URL will be: `https://your-app.railway.app`

---

## Deploy Frontend → Vercel

1. Go to [vercel.com](https://vercel.com) → New Project → Import from GitHub
2. Set **Root Directory** to `frontend`
3. Set environment variable:

```
VITE_API_URL=https://your-app.railway.app
```

Vercel auto-detects Vite and runs `npm run build`.

---

## Database Setup (Supabase)

Run these SQL files in **Supabase → SQL Editor** in order:

1. `schema.sql` — creates base tables
2. `whatsapp_migration.sql` — adds WhatsApp tables + phone numbers
3. `add_customer_fields.sql` — adds customer info columns
4. `fix_status_constraint.sql` — drops old status constraint
5. `seed_technicians.sql` — adds 15 realistic technicians (optional)

---

## Run WhatsApp Automation Locally

WhatsApp Playwright automation **cannot run on Railway** — it needs a real browser.
Run it on your local machine or a VPS with a display.

```bash
# Install all deps including playwright
pip install -r requirements.txt
pip install -r requirements.local.txt
playwright install chromium

# Copy and fill in your env
cp .env.example .env

# Enable WhatsApp locally
# Set ENABLE_WHATSAPP=true in .env

python main.py
```

The backend will open a Chromium window. Scan the WhatsApp QR code once.
Session is saved to `whatsapp_session_2/` — you only scan once.

---

## Local Development

```bash
# Backend
pip install -r requirements.txt
pip install -r requirements.local.txt
cp .env.example .env   # fill in values
python main.py

# Frontend (separate terminal)
cd frontend
npm install
cp .env.example .env   # set VITE_API_URL=http://localhost:8000
npm run dev
```
