# MediaFire 批量下载器 2.0 —— 首次使用设置
# 做三件事：① 去掉 Windows 给"从网上下载的文件"加的锁定标记
#            ② 在桌面创建一个快捷方式
#            ③ 启动程序
# 之后你只要双击桌面上的快捷方式，就不会再看到"Windows 已保护你的电脑"了。

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ""
Write-Host "  MediaFire 批量下载器 2.0 - 首次使用设置" -ForegroundColor Cyan
Write-Host "  ----------------------------------------" -ForegroundColor DarkGray
Write-Host ""

$exe = Get-ChildItem -Path $root -Filter "*.exe" -File | Select-Object -First 1
if (-not $exe) {
    Write-Host "  [x] 这个文件夹里找不到程序主文件（.exe）。" -ForegroundColor Red
    Write-Host "      请把本文件放在解压出来的文件夹里，和 exe 同一层。"
    Write-Host ""
    Read-Host "  按回车键退出"
    exit 1
}

Write-Host "  [1/3] 解除文件锁定标记（这样双击时不会再弹安全提示）..."
$files = Get-ChildItem -Path $root -Recurse -File
$ok = 0
foreach ($f in $files) {
    try { Unblock-File -LiteralPath $f.FullName -ErrorAction Stop; $ok++ } catch {}
}
Write-Host "        已处理 $ok / $($files.Count) 个文件。" -ForegroundColor Green

Write-Host "  [2/3] 在桌面创建快捷方式..."
try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if (-not $desktop) { $desktop = Join-Path $env:USERPROFILE "Desktop" }
    $lnk = Join-Path $desktop "MediaFire 批量下载器 2.0.lnk"
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $exe.FullName
    $s.WorkingDirectory = $root
    $s.IconLocation = $exe.FullName + ",0"
    $s.Description = "MediaFire 批量下载器 2.0"
    $s.Save()
    Write-Host "        桌面快捷方式已创建。" -ForegroundColor Green
} catch {
    Write-Host "        快捷方式没建成（不影响程序本身）：$($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "  [3/3] 启动程序..."
Start-Process -FilePath $exe.FullName -WorkingDirectory $root
Write-Host ""
Write-Host "  设置完成。以后直接双击桌面上的「MediaFire 批量下载器 2.0」就行。" -ForegroundColor Green
Write-Host "  这个窗口 3 秒后自动关闭。" -ForegroundColor DarkGray
Start-Sleep -Seconds 3