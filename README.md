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

### 1. Environment
```bash
cp .env.example .env
# Fill in required values (see .env.example comments)
```

### 2. Start local services
```bash
docker compose up -d
```

### 3. Run database migrations
```bash
cd db && alembic upgrade head
```

### 4. Seed reference data
```bash
psql $DATABASE_URL -f db/seeds/canonical_fields.sql
psql $DATABASE_URL -f db/seeds/field_aliases.sql
psql $DATABASE_URL -f db/seeds/source_configs.sql
```

### 5. Start API (development)
```bash
cd apps/api && uvicorn main:app --reload --port 8000
```

### 6. Start frontend (development)
```bash
cd apps/web && npm run dev
```

## Phase 1 Scope
US public companies (SEC/EDGAR), annual financials (10-K), XBRL extraction, Excel export.

## Documentation
- Engineering Specification Part 1–3 (database, backend, frontend, extraction, validation, export)
- Phase 1 Implementation Guide (step-by-step build order)
- Project Requirements (product scope)
