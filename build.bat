@echo off
cd /d "%~dp0"
echo === Focus Wind: build exe + bo cai ===

where python >nul 2>nul || (echo Can cai Python 3 truoc: https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) & pause & exit /b 1)

python -m venv .venv || goto :err
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
pip install -q "pywebview>=5.3" pyinstaller winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams || goto :err

for /f "delims=" %%V in ('python -c "import version;print(version.APP_VERSION)"') do set "APPVER=%%V"
if not defined APPVER (echo Khong doc duoc APP_VERSION trong version.py & goto :err)
echo Phien ban: %APPVER%

rem Goi pyinstaller qua file .spec de giu cau hinh on dinh (khong bi ghi de)
pyinstaller --noconfirm --clean FocusWind.spec || goto :err

rem --- Tim Inno Setup: uu tien ban 7, roi ban 6 ---
set "ISCC="
for /f "delims=" %%I in ('where iscc 2^>nul') do if not defined ISCC set "ISCC=%%I"
for %%P in (
  "D:\file\Inno Setup 7\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe"
  "%ProgramFiles%\Inno Setup 7\ISCC.exe"
  "%LocalAppData%\Programs\Inno Setup 7\ISCC.exe"
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist "%%~P" set "ISCC=%%~P"

if not defined ISCC (
  echo Chua co Inno Setup 6 hoac 7. Tai ban mien phi: https://jrsoftware.org/isdl.php roi chay lai build.bat
  echo Exe da duoc tao tai dist\FocusWind.exe
  pause
  exit /b 0
)

echo Dung trinh bien dich: "%ISCC%"
"%ISCC%" /DAppVersion=%APPVER% installer.iss || goto :err

echo.
echo Xong! File exe: dist\FocusWind.exe   Bo cai: dist\FocusWind-Setup.exe
echo Ma sha256 cua bo cai ^(tuy chon, dan vao latest.json de app kiem tra file tai ve^):
certutil -hashfile dist\FocusWind-Setup.exe SHA256
pause
exit /b 0

:err
echo Build loi, xem thong bao phia tren.
pause
exit /b 1