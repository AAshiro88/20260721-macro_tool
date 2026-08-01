@echo off
rem Macro automation tool - build script
rem Uses Anaconda base environment with PyInstaller to package macro_tool.py
rem Usage: double-click this file to run
setlocal

cd /d "%~dp0"

rem Automatically locate Anaconda / Miniconda installation
set "CONDA_ROOT="
if exist "C:\ProgramData\Anaconda3\Scripts\activate.bat" set "CONDA_ROOT=C:\ProgramData\Anaconda3"
if "%CONDA_ROOT%"=="" if exist "%USERPROFILE%\Anaconda3\Scripts\activate.bat" set "CONDA_ROOT=%USERPROFILE%\Anaconda3"
if "%CONDA_ROOT%"=="" if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" set "CONDA_ROOT=%USERPROFILE%\miniconda3"
if "%CONDA_ROOT%"=="" if exist "C:\ProgramData\miniconda3\Scripts\activate.bat" set "CONDA_ROOT=C:\ProgramData\miniconda3"

if "%CONDA_ROOT%"=="" (
    echo [ERROR] Anaconda or Miniconda was not found.
    echo         Please install Conda first, then run this script again.
    pause
    exit /b 1
)

echo Using Conda root: %CONDA_ROOT%
call "%CONDA_ROOT%\Scripts\activate.bat" base
if errorlevel 1 (
    echo [ERROR] Failed to activate base environment.
    pause
    exit /b 1
)

python --version

rem Check if PyInstaller is installed, install it if missing
where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found, installing...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

rem Check required packages, install them if missing
python -c "import keyboard, pynput, PIL, numpy, cv2, win32gui" 2>nul
if errorlevel 1 (
    echo Missing required packages, installing...
    python -m pip install keyboard pynput pywin32 pillow opencv-python numpy
)

echo Building, please wait...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name MacroTool macro_tool.py

if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo Build complete. The program is located at dist\MacroTool\MacroTool.exe
pause
