@echo off
rem ===================================================================
rem  MediaFire 批量下载器 2.0 —— Windows 一键打包
rem
rem  必须用 Python 3.8 打包，打出来的 exe 才能在 Win7 SP1 上跑。
rem  本 bat 会按顺序找这几个解释器，第一个找到的即用：
rem    1) ..\..\build-env\nuget38\tools\python.exe   （本机便携环境）
rem    2) py -3.8
rem    3) python
rem
rem  产物：..\dist\MediaFire批量下载器2.0.exe
rem ===================================================================
setlocal
cd /d "%~dp0"

set "PY="
if exist "..\..\build-env\nuget38\tools\python.exe" set "PY=..\..\build-env\nuget38\tools\python.exe"

if defined PY goto :havepy
where py >nul 2>nul && (py -3.8 -c "import sys" >nul 2>nul && set "PY=py -3.8")
if defined PY goto :havepy
where python >nul 2>nul && set "PY=python"

:havepy
if not defined PY (
    echo [错误] 没找到 Python 解释器。请安装 Python 3.8 x64，或把便携版放到 ..\..\build-env\nuget38\。
    exit /b 1
)

echo [1/3] 检查 PyInstaller ...
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [错误] 该 Python 里没装 PyInstaller。执行： %PY% -m pip install "pyinstaller==5.13.2"
    exit /b 1
)

echo [2/3] 清理旧产物 ...
if exist "..\build-temp" rd /s /q "..\build-temp"

echo [3/3] 开始打包（约 1-2 分钟）...
%PY% -m PyInstaller --clean --noconfirm ^
    --distpath "..\dist" ^
    --workpath "..\build-temp" ^
    build.spec
if errorlevel 1 (
    echo [失败] 打包出错，请看上面的日志。
    exit /b 1
)

echo.
echo [完成] 产物： ..\dist\MediaFire批量下载器2.0.exe
dir /b "..\dist"
endlocal
