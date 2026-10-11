$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  throw "Run scripts\bootstrap.ps1 first."
}
Push-Location (Join-Path $root "services\api")
& $python -m ruff check app tests
if ($LASTEXITCODE -ne 0) { throw "ruff failed" }
& $python -m pytest
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
Pop-Location
$env:PYTHONPATH = Join-Path $root "services\worker-agent"
& $python -m streetcloud_agent status
if ($LASTEXITCODE -ne 0) { throw "agent status failed" }
Write-Host "Verify finished."
