# ScholarLab — Windows PowerShell 启动脚本
# 用法: .\start.ps1 [start|stop|restart|status|reset-state|fresh]

param([string]$Action = "start")

$BASE = Split-Path -Parent $MyInvocation.MyCommand.Path
$FE   = Join-Path $BASE "frontend"
$LOG  = Join-Path $BASE "logs"
$PIDF = Join-Path $BASE ".pids"
$PORTS_FILE = Join-Path $BASE ".runtime_ports.ps1"

function _py {
    $cfg = Join-Path $BASE "examples\config_template.yaml"
    if (Test-Path $cfg) {
        $m = Select-String -Path $cfg -Pattern 'python_path:\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { $p = $m.Matches[0].Groups[1].Value; if (Test-Path $p) { return $p } }
    }
    $v = Join-Path $BASE "claw-ai-env\Scripts\python.exe"
    if (Test-Path $v) { return $v }
    return "python"
}

if (Test-Path $PORTS_FILE) { . $PORTS_FILE }
$RM_PORT = if ($env:RESOURCE_MONITOR_PORT) { $env:RESOURCE_MONITOR_PORT } elseif ($script:_RM) { $script:_RM } else { "8905" }
$AB_PORT = if ($env:AGENT_BRIDGE_PORT)     { $env:AGENT_BRIDGE_PORT }     elseif ($script:_AB) { $script:_AB } else { "8906" }
$FE_PORT = if ($env:FRONTEND_PORT)         { $env:FRONTEND_PORT }         elseif ($script:_FE) { $script:_FE } else { "5903" }

New-Item -ItemType Directory -Force -Path $LOG  | Out-Null
New-Item -ItemType Directory -Force -Path $PIDF | Out-Null

function _port([string]$p) {
    return ($null -ne (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue))
}

function _start {
    $PY = _py
    Write-Host "Starting ScholarLab..." -ForegroundColor Cyan

    if (_port $RM_PORT) {
        Write-Host "  [skip] resource_monitor already running (port $RM_PORT)" -ForegroundColor Yellow
    } else {
        $env:PYTHONUTF8 = "1"
        $proc = Start-Process -FilePath $PY `
            -ArgumentList "-u `"$BASE\backend\services\resource_monitor.py`" --port $RM_PORT" `
            -RedirectStandardOutput "$LOG\resource_monitor.log" `
            -RedirectStandardError  "$LOG\resource_monitor.err" `
            -WindowStyle Hidden -PassThru
        $proc.Id | Out-File "$PIDF\resource_monitor.pid" -Encoding ascii
        Start-Sleep -Seconds 1
        Write-Host "  [ok] resource_monitor PID=$($proc.Id)" -ForegroundColor Green
    }

    if (_port $AB_PORT) {
        Write-Host "  [skip] agent_bridge already running (port $AB_PORT)" -ForegroundColor Yellow
    } else {
        $bargs = "-u `"$BASE\backend\services\agent_bridge.py`"" +
            " --port $AB_PORT --python `"$PY`"" +
            " --agent-dir `"$BASE\backend\agent`"" +
            " --runs-dir `"$BASE\backend\runs`"" +
            " --pool-idea 3 --pool-exp 2 --pool-code 3 --pool-exec 4 --pool-write 2" +
            " --total-gpus 8 --gpus-per-project 1" +
            " --discussion-mode --discussion-rounds 2" +
            " --discussion-models `"glm-5-turbo,glm-5-turbo`"" 
        $env:PYTHONUTF8 = "1"
        $proc = Start-Process -FilePath $PY `
            -ArgumentList $bargs `
            -RedirectStandardOutput "$LOG\agent_bridge.log" `
            -RedirectStandardError  "$LOG\agent_bridge.err" `
            -WindowStyle Hidden -PassThru
        $proc.Id | Out-File "$PIDF\agent_bridge.pid" -Encoding ascii
        Start-Sleep -Seconds 1
        Write-Host "  [ok] agent_bridge PID=$($proc.Id)" -ForegroundColor Green
    }

    if (_port $FE_PORT) {
        Write-Host "  [skip] frontend already running (port $FE_PORT)" -ForegroundColor Yellow
    } else {
        $env:RESOURCE_MONITOR_PORT = $RM_PORT
        $env:AGENT_BRIDGE_PORT     = $AB_PORT
        $npxCmd = Get-Command npx.cmd -ErrorAction SilentlyContinue
        $npx = if ($npxCmd) { $npxCmd.Source } else { "npx.cmd" }
        $proc = Start-Process -FilePath "cmd.exe" `
            -ArgumentList "/c `"$npx`" vite --host 0.0.0.0 --port $FE_PORT" `
            -WorkingDirectory $FE `
            -RedirectStandardOutput "$LOG\frontend.log" `
            -RedirectStandardError  "$LOG\frontend.err" `
            -WindowStyle Hidden -PassThru
        $proc.Id | Out-File "$PIDF\frontend.pid" -Encoding ascii
        Start-Sleep -Seconds 2
        Write-Host "  [ok] frontend PID=$($proc.Id)" -ForegroundColor Green
    }

    "`$script:_RM=`"$RM_PORT`"`n`$script:_AB=`"$AB_PORT`"`n`$script:_FE=`"$FE_PORT`"" | Out-File $PORTS_FILE -Encoding utf8

    Write-Host ""
    Write-Host "  Frontend:  http://localhost:$FE_PORT/" -ForegroundColor Green
    Write-Host "  WS Monitor: ws://localhost:$RM_PORT"
    Write-Host "  WS Bridge:  ws://localhost:$AB_PORT"
    Write-Host ""
}

function _stop {
    Write-Host "Stopping ScholarLab..." -ForegroundColor Red
    foreach ($svc in @("frontend","agent_bridge","resource_monitor")) {
        $f = "$PIDF\$svc.pid"
        if (Test-Path $f) {
            $id = [int](Get-Content $f -Raw).Trim()
            Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
            Write-Host "  [stopped] $svc PID=$id" -ForegroundColor Green
            Remove-Item $f -Force
        }
    }
    foreach ($port in @($FE_PORT, $RM_PORT, $AB_PORT)) {
        $cs = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($cs) { $cs | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }
    }
    Write-Host ""
}

function _status {
    Write-Host "Service Status:" -ForegroundColor Cyan
    foreach ($pair in @(
        @{n="resource_monitor"; p=$RM_PORT},
        @{n="agent_bridge";     p=$AB_PORT},
        @{n="frontend";         p=$FE_PORT}
    )) {
        if (_port $pair.p) {
            Write-Host "  [UP]   $($pair.n) :$($pair.p)" -ForegroundColor Green
        } else {
            Write-Host "  [DOWN] $($pair.n) :$($pair.p)" -ForegroundColor Red
        }
    }
    Write-Host ""
}

function _reset {
    $RUNS   = Join-Path $BASE "backend\runs"
    $SHARED = Join-Path $BASE "backend\shared_results"
    Write-Host "Resetting pipeline state..." -ForegroundColor Yellow
    foreach ($d in @("$RUNS\projects","$RUNS\queues","$SHARED\idea_runs","$SHARED\idea_pool","$SHARED\knowledge_base","$SHARED\entries")) {
        Remove-Item "$d\*" -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
    Remove-Item "$SHARED\index.json" -Force -ErrorAction SilentlyContinue
    Write-Host "  [ok] Cleared pipeline data (datasets/checkpoints untouched)" -ForegroundColor Green
    Write-Host ""
}

switch ($Action) {
    "start"       { _start }
    "stop"        { _stop }
    "restart"     { _stop; Start-Sleep -Seconds 1; _start }
    "status"      { _status }
    "reset-state" { _reset }
    "fresh"       { _stop; Start-Sleep -Seconds 1; _reset; _start }
    default       { Write-Host "Usage: .\start.ps1 [start|stop|restart|status|reset-state|fresh]" }
}
