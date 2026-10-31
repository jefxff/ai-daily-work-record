<#
AI Daily Work Record —— 一键启动 / 重启 / 停止（Windows PowerShell）

设计要点：
1. web 与调度器始终是两个独立进程（settings.NOTIFICATIONS_SCHEDULER_INPROCESS 保持 False），
   不会退化成「web 内起调度线程」。
2. 两个进程都由 WMI Win32_Process.Create 以隐藏窗口方式拉起（父进程 WmiPrvSE，
   不在当前终端/IDE 的进程树里），脚本执行完即退出；关掉终端不会带走服务
   （不要再手工前台运行 runserver / run_scheduler）。
3. 进程号写入 logs\.project-state.json，stop 时按 PID + 命令行双重兜底清理。
4. restart = 停止 → migrate → createcachetable → collectstatic --clear → 重新拉起。
   因为 runserver 使用 --noreload，改代码后必须 restart 才生效；改静态资源后必须重新归集。
5. 调度器存活判定读 logs\.hb_scheduler 心跳（run_scheduler 每 30s 落盘一次），
   与健康页 /system/health/ 的口径一致。

用法：
  scripts\windows\project.bat start                # 启动（默认重新归集静态资源）
  scripts\windows\project.bat restart              # 重启（改代码 / 改静态后使用）
  scripts\windows\project.bat stop                 # 停止
  scripts\windows\project.bat status               # 查看状态
  scripts\windows\project.bat start -SkipStatic    # 跳过 collectstatic（只改了 .py 时更快）
  scripts\windows\project.bat restart -Port 8090   # 换端口
  直接双击 project.bat（不带参数）= 交互菜单
#>

param(
    [Parameter(Position = 0)]
    [string]$Action = '',
    [int]$Port = 8080,
    [switch]$SkipStatic,
    [switch]$Https
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Manage = Join-Path $Root 'manage.py'
# HTTPS 入口：scripts/serve_https.py（runserver 不支持 --cert-file，见该脚本头部说明）
$ServeHttps = Join-Path $Root 'scripts\serve_https.py'
$CertFile = Join-Path $Root 'certs\server.crt.pem'
$KeyFile = Join-Path $Root 'certs\server.key.pem'
$script:UseHttps = [bool]$Https
$script:Scheme = $(if ($Https) { 'https' } else { 'http' })
$LogDir = Join-Path $Root 'logs'
$StateFile = Join-Path $LogDir '.project-state.json'
$WebOut = Join-Path $LogDir 'web.out'
$WebErr = Join-Path $LogDir 'web.err'
$SchedOut = Join-Path $LogDir 'scheduler.out'
$SchedErr = Join-Path $LogDir 'scheduler.err'

# 心跳有效窗口（秒）：与 notifications/scheduler.py 的 ALIVE_WINDOW_SECONDS 一致。
# start 与 status 必须用同一个阈值，否则会出现「start 认为活着、status 认为死了」。
$script:HeartbeatAliveSeconds = 90

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    $Text" -ForegroundColor Yellow }
function Write-Err([string]$Text) { Write-Host "    $Text" -ForegroundColor Red }

# 解释器解析统一走 detect_python.ps1（唯一实现，start_windows*.bat 也调它）：
# 按「装了 requirements.txt 所要求版本 Django」来挑，挑不到就报错并给出修复命令。
# 早前这里只看 项目内 .venv / venv / C:\Python311 / PATH，虚拟环境放在
# %USERPROFILE%\.venvs\ 时会一路退化成 PATH 里那个没装 Django 的 python.exe。
. (Join-Path $PSScriptRoot 'detect_python.ps1')

function Get-PythonInfo {
    return (Resolve-ProjectPython -Root $Root)
}

function Read-State {
    if (-not (Test-Path $StateFile)) { return $null }
    try { return (Get-Content -Raw -Path $StateFile | ConvertFrom-Json) } catch { return $null }
}

function Save-State($State) {
    $State | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

function Test-CommandLineContains([string]$CommandLine, [string]$Needle) {
    # Windows 路径大小写不敏感，不能用 String.Contains（区分大小写）
    if (-not $CommandLine -or -not $Needle) { return $false }
    return $CommandLine.IndexOf($Needle, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Test-IsProjectProcess([string]$CommandLine) {
    <#
      判断命令行是否属于本项目。manage.py / serve_https.py 都可能以绝对路径或相对路径
      出现：本脚本用绝对路径拉起，手工运行与 start_windows_https.bat 是 cd 到根目录后
      用相对路径（python manage.py …）。早前只认绝对路径，后果是：
        - stop 打印「停止完成」，实际相对路径起的进程全都活着；
        - 于是再 start 一次就多一个调度器，重复发通知、重复跑备份。
    #>
    if (-not $CommandLine) { return $false }
    if (Test-CommandLineContains $CommandLine $Root) { return $true }
    if ($CommandLine -match '(?i)(^|[\s"])manage\.py(\s|$|")') { return $true }
    if ($CommandLine -match '(?i)(^|[\s"])scripts[\\/]serve_https\.py(\s|$|")') { return $true }
    return $false
}

function Test-PidAlive([int]$ProcessId) {
    if (-not $ProcessId) { return $false }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    # 防 PID 复用：命令行必须仍指向本项目的 manage.py / serve_https.py
    return (Test-IsProjectProcess $p.CommandLine)
}

function Get-ProjectProcesses {
    param([string[]]$Patterns)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $cl = $_.CommandLine
            if (-not $cl) { return $false }
            if (-not (Test-IsProjectProcess $cl)) { return $false }
            foreach ($p in $Patterns) { if ($cl -match $p) { return $true } }
            return $false
        }
}

function Get-ProjectInstances {
    <#
      一个实例常常表现为「启动器 + 真实解释器」两级同名进程：本机 venv 的 python.exe
      会再拉起一个子进程（命令行一模一样）。直接枚举会把一个实例数成两个，
      于是 status 看起来像起了两份、stop 还会多打印一行「已不在运行」。
      这里只保留「父进程不在本集合里」的那些行，一个实例只剩一行。
      注意：stop 时必须对保留下来的顶层进程用 taskkill /T，才能连带杀掉子进程。
    #>
    param([string[]]$Patterns)
    $all = @(Get-ProjectProcesses -Patterns $Patterns)
    if ($all.Count -eq 0) { return @() }
    $ids = @($all | ForEach-Object { [int]$_.ProcessId })
    return @($all | Where-Object { $ids -notcontains [int]$_.ParentProcessId })
}

function Test-PortOpen([int]$PortNumber) {
    <# 真实 TCP 探测（不依赖 TCP 表）：本机 Get-NetTCPConnection 会把在监听的端口报成
       OwningProcess=0，只看表会误判成「端口空闲」。默认 1.2 秒超时。 #>
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ok = $client.ConnectAsync('127.0.0.1', $PortNumber).Wait(1200)
        return ($ok -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-PortOwner([int]$PortNumber) {
    # 刚 kill 掉监听进程后，Windows 会留一条 State=Listen、OwningProcess=0（Idle）的
    # 幽灵记录，实际 connect 已经连不上。不过滤掉它就会误判「端口被占用」而拒绝启动。
    $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue |
        Where-Object { [int]$_.OwningProcess -gt 0 } |
        Select-Object -First 1
    if (-not $conn) {
        # 本机实测：8443 明明在监听，Get-NetTCPConnection -LocalPort 却返回空。
        # 端口过滤不可靠时退回全量枚举再筛，否则会误判「端口空闲」而拉起第二个 web。
        $conn = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { [int]$_.LocalPort -eq $PortNumber -and [int]$_.OwningProcess -gt 0 } |
            Select-Object -First 1
    }
    if (-not $conn) {
        # TCP 表里拿不到所有者（实测本机会给出 OwningProcess=0，受限环境里则干脆看不到 PID）。
        # 改用真实 connect 探测：能连上就是被占用，只是监听者身份未知（Pid=0）。
        if (-not (Test-PortOpen $PortNumber)) { return $null }
        return [pscustomobject]@{ Pid = 0; Name = '未知进程' }
    }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Pid  = $conn.OwningProcess
        Name = $(if ($proc) { $proc.ProcessName } else { '?' })
    }
}

function Get-HeartbeatAge([string]$Name) {
    $path = Join-Path $LogDir ".hb_$Name"
    if (-not (Test-Path $path)) { return $null }
    return [math]::Round(((Get-Date) - (Get-Item $path).LastWriteTime).TotalSeconds, 0)
}

function Show-Tail([string]$Path, [int]$Lines = 15) {
    if (-not (Test-Path $Path)) { return }
    $content = Get-Content -Path $Path -Tail $Lines -ErrorAction SilentlyContinue
    if (-not $content) { return }
    Write-Host "    --- $([IO.Path]::GetFileName($Path)) 末尾 $Lines 行 ---" -ForegroundColor DarkGray
    $content | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
}

function Invoke-Manage {
    param([string[]]$Arguments)
    Write-Host "    > python manage.py $($Arguments -join ' ')" -ForegroundColor DarkGray
    & $script:Python $Manage @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败（退出码 $LASTEXITCODE）：manage.py $($Arguments -join ' ')"
    }
}

function Stop-One([int]$ProcessId, [string]$Label) {
    if (-not $ProcessId) { return }
    $exists = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $exists) {
        Write-Host "    $Label（PID $ProcessId）已不在运行" -ForegroundColor DarkGray
        return
    }
    # taskkill 找不到进程时会写 stderr，临时放宽 EAP 避免误抛异常
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & taskkill.exe /PID $ProcessId /T /F 2>&1 | Out-Null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -eq 0) {
        Write-Ok "已停止 $Label（PID $ProcessId）"
    } else {
        Write-Warn "$Label（PID $ProcessId）停止失败（taskkill 退出码 $code），请手工确认。"
    }
}

function Stop-All {
    Write-Step '停止 web 与调度器'
    $state = Read-State
    if ($state) {
        if ($state.webPid) { Stop-One ([int]$state.webPid) 'web' }
        if ($state.schedulerPid) { Stop-One ([int]$state.schedulerPid) 'scheduler' }
    }

    # 兜底：清理没有记录在 state 里、但命令行指向本项目 manage.py / serve_https.py 的残留进程
    $leftover = Get-ProjectInstances -Patterns @('runserver', 'run_scheduler', 'serve_https')
    foreach ($p in $leftover) {
        Stop-One ([int]$p.ProcessId) "残留进程($($p.Name))"
    }

    # 等端口释放
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-PortOwner $Port)) { break }
        Start-Sleep -Milliseconds 500
    }
    $owner = Get-PortOwner $Port
    if ($owner) {
        Write-Warn "端口 $Port 仍被 PID $($owner.Pid)（$($owner.Name)）占用，可能不是本项目进程，请手工确认。"
    }

    if (Test-Path $StateFile) { Remove-Item -Path $StateFile -Force }

    # 停止后清掉心跳文件：否则刚被 kill 的进程心跳仍然「新鲜」，start 会误判调度器还在运行
    foreach ($name in @('.hb_scheduler', '.hb_worker')) {
        $hbPath = Join-Path $LogDir $name
        if (Test-Path $hbPath) { Remove-Item -Path $hbPath -Force -ErrorAction SilentlyContinue }
    }
    Write-Ok '停止完成'
}

function Start-Detached {
    <#
      用 WMI Win32_Process.Create 拉起后台进程（父进程是 WmiPrvSE，不在当前终端/IDE 的进程树里）。
      这是「关掉终端也不会带走服务」的关键：Start-Process 仍是当前 shell 的子进程，
      IDE 关闭终端时按进程树清理会连带杀掉；WMI 创建则不受影响。
    #>
    param(
        [string]$PythonPath,
        [string[]]$Arguments,
        [string]$StdOutPath,
        [string]$StdErrPath
    )

    $quotedArgs = foreach ($a in $Arguments) {
        if ($a -match '[\s"]') { '"' + ($a -replace '"', '\"') + '"' } else { $a }
    }
    $argLine = ($quotedArgs -join ' ')
    $cmdLine = 'cmd.exe /d /s /c ""' + $PythonPath + '" ' + $argLine +
        ' > "' + $StdOutPath + '" 2> "' + $StdErrPath + '""'

    try {
        $startup = ([wmiclass]'Win32_ProcessStartup').CreateInstance()
        $startup.ShowWindow = 0          # SW_HIDE：不弹出控制台窗口
        $result = ([wmiclass]'Win32_Process').Create($cmdLine, $Root, $startup)
        if ($result.ReturnValue -eq 0) {
            return [int]$result.ProcessId
        }
        throw "WMI 返回码 $($result.ReturnValue)"
    } catch {
        # WMI 不可用时退回 Start-Process（此时关终端仍可能被进程树清理）
        Write-Warn "WMI 创建后台进程失败（$($_.Exception.Message)），退回 Start-Process。"
        $proc = Start-Process -FilePath $PythonPath -ArgumentList ($quotedArgs -join ' ') -WorkingDirectory $Root `
            -WindowStyle Hidden -RedirectStandardOutput $StdOutPath -RedirectStandardError $StdErrPath -PassThru
        return [int]$proc.Id
    }
}

function Start-Web([string]$PythonPath) {
    if ($script:UseHttps) {
        return Start-Detached -PythonPath $PythonPath `
            -Arguments @($ServeHttps, '--port', "$Port") `
            -StdOutPath $WebOut -StdErrPath $WebErr
    }
    return Start-Detached -PythonPath $PythonPath `
        -Arguments @($Manage, 'runserver', "0.0.0.0:$Port", '--noreload') `
        -StdOutPath $WebOut -StdErrPath $WebErr
}

function Start-Scheduler([string]$PythonPath) {
    return Start-Detached -PythonPath $PythonPath `
        -Arguments @($Manage, 'run_scheduler') `
        -StdOutPath $SchedOut -StdErrPath $SchedErr
}

function Start-All {
    $py = Get-PythonInfo
    $script:Python = $py.Path
    $modeText = $(if ($script:UseHttps) { 'HTTPS' } else { 'HTTP' })
    Write-Step "启动项目（$modeText，python: $($py.Path)）"
    Write-Ok "django $($py.DjangoVersion)（来源：$($py.Why)）"
    if (-not $py.Exact) {
        Write-Warn "Django 版本与 requirements.txt 不一致，建议 pip install -r requirements.txt。"
    }

    if ($script:UseHttps) {
        if (-not ((Test-Path $CertFile) -and (Test-Path $KeyFile))) {
            throw "HTTPS 模式需要证书，但未找到 $CertFile / $KeyFile。先运行：python scripts\gen_dev_cert.py"
        }
    }

    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

    $state = Read-State
    $webPid = $(if ($state) { [int]$state.webPid } else { 0 })
    $schedPid = $(if ($state) { [int]$state.schedulerPid } else { 0 })
    $webAlive = Test-PidAlive $webPid
    $schedAge = Get-HeartbeatAge 'scheduler'
    $schedProcesses = @(Get-ProjectInstances -Patterns @('run_scheduler'))
    $schedAlive = (Test-PidAlive $schedPid) -or ($schedProcesses.Count -gt 0) -or
        (($null -ne $schedAge) -and ($schedAge -lt $script:HeartbeatAliveSeconds))

    if ($webAlive -and $schedAlive) {
        Write-Ok 'web 与调度器都已在运行，无需重复启动；改代码后请用 restart。'
        Show-Status
        return
    }

    # 端口被占时先分情况：本项目自己的实例（例如之前用 -Https 起的）当「已在运行」，
    # 别人的进程才报错——否则改个模式启动就会卡在「端口被占用」上无法自愈。
    $owner = Get-PortOwner $Port
    if ($owner -and -not $webAlive) {
        $inst = @(Get-ProjectInstances -Patterns @('runserver', 'serve_https'))
        if ($inst.Count -gt 0) {
            Write-Warn "端口 $Port 已有本项目实例在监听（PID $($inst[0].ProcessId)）：按「已在运行」处理，不重复拉起。"
            $webAlive = $true
            $webPid = [int]$inst[0].ProcessId
        } else {
            $who = $(if ($owner.Pid -gt 0) { "PID $($owner.Pid)（$($owner.Name)）" } else { '一个身份不可读的进程' })
            throw "端口 $Port 已被 $who 占用（可能是手工启动的旧进程），请先关闭它。"
        }
    }

    # ---- 代码/数据库/静态资源准备 ----
    # web 已在运行时不再动 DB / staticfiles，避免运行中出现短暂不一致；这种情况只补调度器。
    if ($webAlive) {
        Write-Warn 'web 已在运行：跳过 migrate / collectstatic，仅补齐调度器；需要应用迁移或重归集静态请用 restart。'
    } else {
        Invoke-Manage @('migrate', '--noinput')
        Invoke-Manage @('createcachetable')
        if ($SkipStatic) {
            Write-Warn '已跳过 collectstatic（-SkipStatic）。改了 static/ 下的资源请改用 restart。'
        } else {
            Invoke-Manage @('collectstatic', '--noinput', '--clear')
        }
    }

    # ---- web ----
    if ($webAlive) {
        Write-Warn "web 已在运行（PID $webPid），跳过启动。"
    } else {
        $owner = Get-PortOwner $Port
        if ($owner) {
            throw "端口 $Port 已被 PID $($owner.Pid)（$($owner.Name)）占用，无法启动 web。"
        }
        $webPid = Start-Web $script:Python
        Write-Ok "web 已启动（PID $webPid，端口 $Port）"

        $listening = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            if (Get-PortOwner $Port) { $listening = $true; break }
            if (-not (Test-PidAlive $webPid)) { break }
        }
        if (-not $listening) {
            Write-Err 'web 未能在 15 秒内监听端口，启动失败。'
            Show-Tail $WebErr
            Show-Tail $WebOut
            throw 'web 启动失败，请查看上面的日志尾部。'
        }
    }

    # ---- 调度器 ----
    if ($schedAlive) {
        Write-Warn '调度器心跳仍在有效窗口内，跳过启动（避免双调度进程）。'
    } else {
        $schedPid = Start-Scheduler $script:Python
        Write-Ok "调度器已启动（PID $schedPid）"

        $fresh = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            if (-not (Test-PidAlive $schedPid)) { break }
            $age = Get-HeartbeatAge 'scheduler'
            if (($null -ne $age) -and ($age -lt 10)) { $fresh = $true; break }
        }
        if (-not $fresh) {
            Write-Warn '调度器 15 秒内未写出心跳，请检查 logs\scheduler.err 与 logs\debug.log。'
            Show-Tail $SchedErr
            Show-Tail $SchedOut
        }
    }

    Save-State ([pscustomobject]@{
        webPid       = $webPid
        schedulerPid = $schedPid
        port         = $Port
        mode         = $(if ($script:UseHttps) { 'https' } else { 'http' })
        startedAt    = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    })

    Write-Step '启动完成'
    Write-Ok "访问地址：$($script:Scheme)://127.0.0.1:$Port/   健康页：$($script:Scheme)://127.0.0.1:$Port/system/health/"
}

function Show-Status {
    $state = Read-State
    Write-Step '项目状态'

    if ($state) {
        $stateMode = $(if ($state.mode) { "$($state.mode)" } else { 'http' })
        $currentMode = $(if ($script:UseHttps) { 'https' } else { 'http' })
        if ($stateMode -eq $currentMode) {
            Write-Host "    端口: $([int]$state.port)    启动时间: $($state.startedAt)"
        } else {
            Write-Host "    上次是按 $stateMode 端口 $([int]$state.port) 启动（$($state.startedAt)）；当前按 $currentMode 端口 $Port 查看" -ForegroundColor DarkGray
        }
    }

    $webPid = $(if ($state) { [int]$state.webPid } else { 0 })
    $owner = Get-PortOwner $Port
    if ($owner) {
        $ours = Test-PidAlive $webPid
        $ownerPid = [int]$owner.Pid
        if ($ownerPid -le 0) {
            # TCP 表里拿不到所有者（本机会报成 PID 0）：用本项目 web 实例的 PID 兜底显示
            $inst = @(Get-ProjectInstances -Patterns @('runserver', 'serve_https'))
            if ($inst.Count -gt 0) { $ownerPid = [int]$inst[0].ProcessId; $ours = $true }
        }
        Write-Ok "web: 运行中（$($script:Scheme) 端口 $Port，PID $ownerPid）$(if (-not $ours) { '（非本脚本记录，可能是手工启动）' })"
    } else {
        Write-Err "web: 未运行（$($script:Scheme) 端口 $Port 未监听）"
        # 另一种模式在跑时，别只报「未运行」让人误以为服务挂了
        foreach ($p in @(Get-ProjectInstances -Patterns @('runserver', 'serve_https'))) {
            $label = $(if ($p.CommandLine -match 'serve_https') { 'HTTPS' } else { 'HTTP' })
            Write-Warn "  └ 但检测到本项目有一个 $label 实例在跑（PID $($p.ProcessId)）；查看/停止它请带上对应 -Https 参数。"
        }
    }

    $schedPid = $(if ($state) { [int]$state.schedulerPid } else { 0 })
    $age = Get-HeartbeatAge 'scheduler'
    $pidOk = Test-PidAlive $schedPid
    if (($null -ne $age) -and ($age -lt $script:HeartbeatAliveSeconds) -and $pidOk) {
        Write-Ok "scheduler: 运行中（PID $schedPid，心跳 ${age}s 前）"
    } elseif (($null -ne $age) -and ($age -lt $script:HeartbeatAliveSeconds)) {
        Write-Ok "scheduler: 运行中（心跳 ${age}s 前，PID 非本脚本记录）"
    } else {
        $text = $(if ($null -eq $age) { '无心跳文件' } else { "心跳已是 ${age}s 前" })
        Write-Err "scheduler: 未运行（$text）—— 定时任务不会执行"
    }

    $workerAge = Get-HeartbeatAge 'worker'
    if (($null -ne $workerAge) -and ($workerAge -lt $script:HeartbeatAliveSeconds)) {
        Write-Ok "通知发送 worker: 运行中（心跳 ${workerAge}s 前）"
    } else {
        Write-Warn "通知发送 worker: 未见有效心跳"
    }

    $leftover = Get-ProjectInstances -Patterns @('runserver', 'run_scheduler', 'serve_https')
    if ($leftover) {
        Write-Host "    相关进程：" -ForegroundColor DarkGray
        foreach ($p in $leftover) {
            Write-Host "      PID $($p.ProcessId)  $($p.Name)  $($p.CommandLine)" -ForegroundColor DarkGray
        }
    }
}

function Show-Menu {
    while ($true) {
        Write-Host ''
        Write-Host '======== AI Daily Work Record 一键运维 ========' -ForegroundColor Cyan
        Write-Host '  1) 启动          (start)'
        Write-Host '  2) 重启          (restart，改代码/静态后使用)'
        Write-Host '  3) 停止          (stop)'
        Write-Host '  4) 查看状态      (status)'
        Write-Host '  5) 退出'
        Write-Host '  6) 重启 HTTPS    (restart -Https，内网自签 8443)'
        Write-Host '  7) 状态 HTTPS    (status -Https)'
        Write-Host '================================================' -ForegroundColor Cyan
        $choice = Read-Host '请输入序号'
        switch (("$choice").Trim()) {
            '1' { Start-All }
            '2' { Stop-All; Start-All }
            '3' { Stop-All }
            '4' { Show-Status }
            '5' { return }
            '6' {
                $script:UseHttps = $true; $script:Scheme = 'https'; $script:Port = 8443
                Stop-All; Start-All
            }
            '7' {
                $script:UseHttps = $true; $script:Scheme = 'https'; $script:Port = 8443
                Show-Status
            }
            default { Write-Warn '无效的序号，请重新输入。' }
        }
    }
}

# 未显式传 -Port 时，沿用状态文件记录的端口（且仅当模式一致）：
# 否则 start -Port 8090 之后直接跑 status / stop，会去查默认的 8080 而误报。
if (-not $PSBoundParameters.ContainsKey('Port')) {
    $persistedState = Read-State
    $persistedMode = $(if ($persistedState -and $persistedState.mode) { "$($persistedState.mode)" } else { 'http' })
    if ($persistedState -and $persistedState.port -and $persistedMode -eq $(if ($Https) { 'https' } else { 'http' })) {
        $Port = [int]$persistedState.port
    } elseif ($Https) {
        $Port = 8443   # HTTPS 默认端口，与 scripts/serve_https.py 保持一致
    }
}

switch (("$Action").Trim().ToLower()) {
    'start'   { Start-All }
    'restart' { Stop-All; Start-All }
    'stop'    { Stop-All }
    'status'  { Show-Status }
    ''        { Show-Menu }
    default {
        Write-Err "未知动作：$Action"
        Write-Host '用法：project.bat [start|restart|stop|status] [-Port 8080] [-SkipStatic]'
        exit 1
    }
}
