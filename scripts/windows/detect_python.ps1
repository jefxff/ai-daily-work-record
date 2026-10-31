<#
AI Daily Work Record —— 解析 Python 解释器（Windows PowerShell）

为什么单独抽成一个文件：
    项目的虚拟环境不一定在项目根目录（本机就放在 %USERPROFILE%\.venvs\ 下）。
    project.ps1 与 start_windows*.bat 各自找 python 时，候选列表只有
    「项目内 .venv / venv → C:\Python311 → PATH 里的 python.exe」，
    于是必然退化成 PATH 里第一个 python.exe——那不是装了 Django 的那个，启动直接死在
        ModuleNotFoundError: No module named 'django'
    这里做成唯一实现：按「装了 requirements.txt 所要求版本的 Django」来挑，
    挑不到就明确报错并给出可复制的修复命令，绝不静默选一个不能用的解释器。

用法：
    . <本文件>                                             # 仅载入函数（project.ps1 用）
    powershell -NoProfile -File detect_python.ps1 -Print     # 打印路径，供 .bat 的 for /f 取用
#>

param([switch]$Print)

$ErrorActionPreference = 'Stop'

function Get-RequiredDjangoVersion {
    <# 从 requirements.txt 读 django==x.y.z；读不到返回空串（表示不校验版本）。 #>
    param([string]$Root)
    $req = Join-Path $Root 'requirements.txt'
    if (-not (Test-Path $req)) { return '' }
    foreach ($line in @(Get-Content -Path $req -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*django\s*==\s*([0-9][^\s#;,]*)') { return $Matches[1].Trim() }
    }
    return ''
}

function Get-DjangoVersionOf {
    <# 返回该解释器的 Django 版本；没装或跑不起来返回空串。 #>
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path $Exe)) { return '' }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe -c 'import django,sys;sys.stdout.write(django.get_version())' 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        return ("$out").Trim()
    } catch {
        return ''
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Get-PythonCandidatePaths {
    <# 候选解释器，按「越可能是本项目在用的」排序。 #>
    param([string]$Root)
    $out = New-Object System.Collections.Generic.List[object]

    if ($env:WR_PYTHON) {
        $out.Add([pscustomobject]@{ Path = $env:WR_PYTHON; Why = '环境变量 WR_PYTHON' })
    }
    foreach ($rel in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
        $out.Add([pscustomobject]@{ Path = (Join-Path $Root $rel); Why = "项目内虚拟环境 $rel" })
    }
    # 用户级 venv 目录：虚拟环境没建在项目里时的实际位置
    foreach ($name in @('.venvs', 'venvs')) {
        if (-not $env:USERPROFILE) { continue }
        $base = Join-Path $env:USERPROFILE $name
        if (-not (Test-Path $base)) { continue }
        foreach ($dir in @(Get-ChildItem -Path $base -Directory -ErrorAction SilentlyContinue)) {
            $out.Add([pscustomobject]@{
                Path = (Join-Path $dir.FullName 'Scripts\python.exe')
                Why  = "用户虚拟环境 %USERPROFILE%\$name\$($dir.Name)"
            })
        }
    }
    foreach ($v in @('313', '312', '311', '310')) {
        $out.Add([pscustomobject]@{ Path = "C:\Python$v\python.exe"; Why = "系统安装目录 C:\Python$v" })
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $out.Add([pscustomobject]@{ Path = $cmd.Source; Why = 'PATH 中的 python.exe' }) }

    return $out
}

function Resolve-ProjectPython {
    <#
      返回 [pscustomobject]@{ Path; DjangoVersion; Why; Exact }
      Exact=$true 表示 Django 版本与 requirements.txt 完全一致。
      一个可用的都没有则 throw，异常消息里带完整排查过程和修复命令。
    #>
    param([string]$Root)

    $want = Get-RequiredDjangoVersion -Root $Root
    $candidates = Get-PythonCandidatePaths -Root $Root

    $seen = New-Object System.Collections.Generic.List[string]
    $probed = New-Object System.Collections.Generic.List[object]
    $fallback = $null

    foreach ($c in $candidates) {
        if (-not (Test-Path $c.Path)) { continue }
        $full = (Resolve-Path -LiteralPath $c.Path).Path
        if ($seen -contains $full) { continue }
        $seen.Add($full)

        $ver = Get-DjangoVersionOf -Exe $full
        $probed.Add([pscustomobject]@{ Path = $full; Django = $ver; Why = $c.Why })
        if (-not $ver) { continue }

        if ($want -and $ver -eq $want) {
            return [pscustomobject]@{ Path = $full; DjangoVersion = $ver; Why = $c.Why; Exact = $true }
        }
        # 版本不完全一致也先留一个：总比「没有 Django」强，调用方可以据此告警
        if (-not $fallback) {
            $fallback = [pscustomobject]@{ Path = $full; DjangoVersion = $ver; Why = $c.Why; Exact = $false }
        }
    }

    if ($fallback) { return $fallback }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('没有找到装了 Django 的 Python 解释器，启动中止。')
    if ($want) { $lines.Add("    需要版本：django==$want（读自 requirements.txt）") }
    if ($probed.Count -gt 0) {
        $lines.Add('    已排查：')
        foreach ($p in $probed) {
            $state = $(if ($p.Django) { "django $($p.Django)" } else { '未安装 django' })
            $lines.Add("      - $($p.Path)  [$state]  （来源：$($p.Why)）")
        }
    } else {
        $lines.Add('    已排查：一个 python.exe 都没找到（PATH、%USERPROFILE%\.venvs、C:\Python3xx 均为空）。')
    }
    $lines.Add('    处理办法（任选其一）：')
    $lines.Add('      1) 在项目里建虚拟环境并装依赖：')
    $lines.Add("           python -m venv `"$Root\.venv`"")
    $lines.Add("           `"$Root\.venv\Scripts\python.exe`" -m pip install -r `"$Root\requirements.txt`"")
    $lines.Add('      2) 指向已有的虚拟环境（本机现在就是这种方式）：')
    $lines.Add('           set WR_PYTHON=C:\Users\<你>\.venvs\ai-daily-work-record-py313\Scripts\python.exe')
    throw ($lines -join [Environment]::NewLine)
}

if ($Print) {
    $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    try {
        $info = Resolve-ProjectPython -Root $root
        [Console]::Error.WriteLine("    python: $($info.Path)  (django $($info.DjangoVersion)，来源：$($info.Why))")
        [Console]::Out.WriteLine($info.Path)
        exit 0
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
