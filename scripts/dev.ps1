<#
.SYNOPSIS
  Starts the PTP backend (FastAPI, port 8000) and frontend (Vite dev server) in separate windows.
.NOTES
  The backend runs as a single process without --reload or --workers (lock file backend/data/backend.lock).
  The Vite dev server proxies /api to http://127.0.0.1:8000.
#>
$ErrorActionPreference = 'Stop'

$root     = Split-Path -Parent $PSScriptRoot
$backend  = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'

if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location $frontend
    try { npm install } finally { Pop-Location }
}

Start-Process pwsh -WorkingDirectory $backend -ArgumentList @(
    '-NoExit', '-Command',
    '$Host.UI.RawUI.WindowTitle = "PTP backend"; uv run uvicorn app.main:app --host 127.0.0.1 --port 8000'
)

Start-Process pwsh -WorkingDirectory $frontend -ArgumentList @(
    '-NoExit', '-Command',
    '$Host.UI.RawUI.WindowTitle = "PTP frontend"; npm run dev'
)

Write-Host 'Backend:  http://127.0.0.1:8000'
Write-Host 'Frontend: http://localhost:5173 (Vite default; see its window for the actual URL)'
