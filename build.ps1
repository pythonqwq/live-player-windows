param(
    [switch]$SkipClean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$vlcRoot = Join-Path $root 'vendor-cache\unpacked\vlc-3.0.24'
$required = @(
    (Join-Path $vlcRoot 'libvlc.dll'),
    (Join-Path $vlcRoot 'libvlccore.dll'),
    (Join-Path $vlcRoot 'plugins'),
    (Join-Path $root 'assets\logo.png'),
    (Join-Path $root 'assets\player.ico')
)
foreach ($item in $required) {
    if (-not (Test-Path -LiteralPath $item)) { throw "缺少构建依赖：$item。请按 README.md 准备 VLC 3.0.24 和 Python 依赖。" }
}

$pyInstallerArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--onefile', '--windowed',
    '--name', '直播播放器',
    '--distpath', (Join-Path $root 'dist'),
    '--workpath', (Join-Path $root 'build'),
    '--specpath', $root,
    '--icon', (Join-Path $root 'assets\player.ico'),
    '--add-data', "$(Join-Path $root 'assets');assets",
    '--add-binary', "$(Join-Path $vlcRoot 'libvlc.dll');vlc",
    '--add-binary', "$(Join-Path $vlcRoot 'libvlccore.dll');vlc",
    '--add-data', "$(Join-Path $vlcRoot 'plugins');vlc/plugins",
    '--add-data', "$(Join-Path $vlcRoot 'COPYING.txt');vlc",
    '--add-data', "$(Join-Path $vlcRoot 'AUTHORS.txt');vlc",
    (Join-Path $root 'main.py')
)
if (-not $SkipClean) { $pyInstallerArgs = @('-m', 'PyInstaller', '--clean') + $pyInstallerArgs[2..($pyInstallerArgs.Count - 1)] }
& python @pyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败，退出码 $LASTEXITCODE" }

$exe = Join-Path $root 'dist\直播播放器.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw '没有生成 EXE。' }
Copy-Item -LiteralPath (Join-Path $root '使用说明.txt') -Destination (Join-Path $root 'dist\使用说明.txt') -Force
Copy-Item -LiteralPath (Join-Path $root '第三方许可.txt') -Destination (Join-Path $root 'dist\第三方许可.txt') -Force
Get-Item -LiteralPath $exe | Select-Object FullName,Length
Get-FileHash -LiteralPath $exe -Algorithm SHA256 | Select-Object Hash
