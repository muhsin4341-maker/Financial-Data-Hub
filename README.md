# Financial Data Hub

Production-grade financial data acquisition and Excel export system.

## Architecture
See `/docs/` or the Engineering Specification Parts 1–3 in the project documents folder.

## Prerequisites
- Python 3.12+
- Node.js 20 LTS
- Docker Desktop
- uv package manager (`pip install uv`)

## Quick Start

> **Windows / PowerShell users:** all commands below are PowerShell-safe.
> `&&` is not valid in PowerShell 5.1 — use the forms shown here.
> `psql` and `alembic` are not installed locally; they run inside Docker.

### 1. Environment
```powershell
Copy-Item .env.example .env
# Fill in required values (see .env.example comments)
```

### 2. Start local services
```powershell
docker compose up -d
```

### 3. Run database migrations
```powershell
docker compose exec api alembic -c db/alembic.ini upgrade head
```

### 4. Seed reference data
```powershell
Get-Content db/seeds/canonical_fields.sql | docker compose exec -T db psql -U fdh -d fdh
Get-Content db/seeds/field_aliases.sql    | docker compose exec -T db psql -U fdh -d fdh
Get-Content db/seeds/source_configs.sql   | docker compose exec -T db psql -U fdh -d fdh
```

### 5. Start API (development)
The API runs inside Docker — no local uvicorn needed:
```powershell
docker compose up -d
# API is available at http://localhost:8000
# Logs: docker compose logs -f api
```

### 6. Start frontend (development)
```powershell
cd apps/web
npm run dev
```

## Phase 1 Scope
US public companies (SEC/EDGAR), annual financials (10-K), XBRL extraction, Excel export.

## Documentation
- Engineering Specification Part 1–3 (database, backend, frontend, extraction, validation, export)
- Phase 1 Implementation Guide (step-by-step build order)
- Project Requirements (product scope)
