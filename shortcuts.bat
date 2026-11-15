@echo off
setlocal

cd /d "%~dp0"
set "SRT_BURNER_RUN=%~dp0Run.bat"

echo Where should the HK's SRT Burner shortcut be created?
echo [1] Desktop
echo [2] Start Menu
echo [3] Both
choice /c 123 /n /m "Choose an option: "

if errorlevel 3 (
    set "SRT_BURNER_DESKTOP=1"
    set "SRT_BURNER_START_MENU=1"
) else if errorlevel 2 (
    set "SRT_BURNER_START_MENU=1"
) else (
    set "SRT_BURNER_DESKTOP=1"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$target=$env:SRT_BURNER_RUN; $shell=New-Object -ComObject WScript.Shell; $locations=@(); if($env:SRT_BURNER_DESKTOP){$locations += Join-Path ([Environment]::GetFolderPath('Desktop')) 'HK''s SRT Burner.lnk'}; if($env:SRT_BURNER_START_MENU){$locations += Join-Path (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs') 'HK''s SRT Burner.lnk'}; foreach($path in $locations){$shortcut=$shell.CreateShortcut($path); $shortcut.TargetPath=$target; $shortcut.WorkingDirectory=Split-Path $target; $shortcut.Description='HK''s SRT Burner'; $shortcut.Save(); Write-Host ('Created: ' + $path)}"

if errorlevel 1 (
    echo Failed to create the shortcut.
) else (
    echo Shortcut creation complete.
)
pause
endlocal
