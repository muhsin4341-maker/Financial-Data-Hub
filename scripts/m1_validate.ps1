# M1 Gate Validation Script
# Run from repo root: .\scripts\m1_validate.ps1

param(
    [string]$DbUrl = "postgresql+asyncpg://fdh:fdh_password@localhost:5432/fdh",
    [string]$DbUrlSync = "postgresql://fdh:fdh_password@localhost:5432/fdh"
)

$ErrorActionPreference = "Stop"
$env:DATABASE_URL = $DbUrl
$env:DATABASE_URL_SYNC = $DbUrlSync

Write-Host "`n=== M1 GATE VALIDATION ===" -ForegroundColor Cyan

# ── 1. Unit tests ─────────────────────────────────────────────────────────────
Write-Host "`n[1/5] Running unit tests..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q --tb=short --no-header
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: Unit tests" -ForegroundColor Red; exit 1 }
Write-Host "PASS: Unit tests" -ForegroundColor Green

# ── 2. Integration tests ──────────────────────────────────────────────────────
Write-Host "`n[2/5] Running integration tests..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe -m pytest tests/integration/ -v --tb=short --no-header --no-cov
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: Integration tests" -ForegroundColor Red; exit 1 }
Write-Host "PASS: Integration tests" -ForegroundColor Green

# ── 3. Health check ───────────────────────────────────────────────────────────
Write-Host "`n[3/5] Checking /health/ready..." -ForegroundColor Yellow
$health = Invoke-RestMethod -Uri "http://localhost:8000/health/ready" -Method GET -ErrorAction Stop
Write-Host "PASS: /health/ready -> $($health | ConvertTo-Json -Compress)" -ForegroundColor Green

# ── 4. E2E auth flow ─────────────────────────────────────────────────────────
Write-Host "`n[4/5] End-to-end auth flow..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe scripts/e2e_auth.py
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: E2E auth flow" -ForegroundColor Red; exit 1 }
Write-Host "PASS: E2E auth flow" -ForegroundColor Green

# ── 5. Password reset flow ────────────────────────────────────────────────────
Write-Host "`n[5/5] Password reset flow..." -ForegroundColor Yellow
.\.venv\Scripts\python.exe scripts/e2e_password_reset.py
if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: Password reset flow" -ForegroundColor Red; exit 1 }
Write-Host "PASS: Password reset flow" -ForegroundColor Green

Write-Host "`n=== ALL M1 GATE CHECKS PASSED ===" -ForegroundColor Green
