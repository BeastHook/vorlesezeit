@echo off
rem Vorlesezeit installieren (Windows). Doppelklick, danach geht es im Browser
rem weiter (One-Klick-Setup, Baustein 3). Beim ersten Oeffnen warnt SmartScreen:
rem "Weitere Informationen", dann "Trotzdem ausfuehren".
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
rem Mit LF-Zeilenenden (Download ueber die Raw-Adresse) findet cmd.exe Sprungmarken
rem falsch. Deshalb laeuft immer eine Kopie mit CRLF. Bis hier keine Sprungmarke.
if not defined VORLESEZEIT_CRLF (
  set "VORLESEZEIT_CRLF=1"
  type "%~f0" | find /v "" >"%TEMP%\vorlesezeit-installieren.bat"
  call "%TEMP%\vorlesezeit-installieren.bat"
  exit /b
)
set "VERSION=0.4.0"
set "IMAGE=ghcr.io/beasthook/vorlesezeit-installer:%VERSION%"
set "ORDNER=%USERPROFILE%\Vorlesezeit"

where docker >nul 2>&1 || goto :ohne_docker

rem Docker Desktop braucht WSL2.
wsl --status >nul 2>&1 || goto :ohne_wsl

docker info >nul 2>&1 && goto :docker_laeuft
echo Docker Desktop startet, das dauert bis zu 2 Minuten ...
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set /a VERSUCHE=0
:warten
timeout /t 5 /nobreak >nul
docker info >nul 2>&1 && goto :docker_laeuft
set /a VERSUCHE+=1
if !VERSUCHE! lss 24 goto :warten
echo Docker Desktop startet nicht. Oeffne es bitte von Hand und danach diese Datei noch einmal.
goto :ende

:docker_laeuft
rem Die Kopie im Installationsordner ist spaeter der Eingang zur Wartung.
if not exist "%ORDNER%" mkdir "%ORDNER%"
if /i not "%~f0"=="%ORDNER%\Vorlesezeit installieren.bat" copy /y "%~f0" "%ORDNER%\Vorlesezeit installieren.bat" >nul

rem 32 Zufallsbytes = 256 Bit, ueber die Umgebung statt als Argument uebergeben.
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "$b = New-Object byte[] 32; (New-Object Security.Cryptography.RNGCryptoServiceProvider).GetBytes($b); -join $b.ForEach({ $_.ToString('x2') })"`) do set "INSTALLER_TOKEN=%%T"
if not defined INSTALLER_TOKEN goto :ohne_token

rem Jede vorhandene Laufwerkswurzel ausser C: nur lesend.
set "LAUFWERKE="
for %%L in (A B D E F G H I J K L M N O P Q R S T U V W X Y Z) do if exist %%L:\ set "LAUFWERKE=!LAUFWERKE! -v %%L:\:/laufwerke/%%L:ro"

echo Der Assistent wird geladen ...
docker pull "%IMAGE%" >nul || goto :ohne_image

rem Erster freier Port ab 8099: Docker meldet einen belegten Port beim Start.
set /a PORT=8099
:starten
docker rm -f vorlesezeit-installer >nul 2>&1
docker run -d --rm --name vorlesezeit-installer -p "127.0.0.1:!PORT!:8000" -v /var/run/docker.sock:/var/run/docker.sock -v "%ORDNER%:/install" !LAUFWERKE! -e INSTALLER_TOKEN -e HOST_OS=windows -e "HOST_INSTALL_DIR=%ORDNER%" -e "INSTALLER_IMAGE=%IMAGE%" "%IMAGE%" >nul 2>"%TEMP%\vorlesezeit-start.txt" && goto :gestartet
findstr /c:"already allocated" /c:"address already in use" /c:"Only one usage" "%TEMP%\vorlesezeit-start.txt" >nul || goto :startfehler
set /a PORT+=1
if !PORT! leq 8199 goto :starten
echo Zwischen 8099 und 8199 ist kein Port frei.
goto :ende

:gestartet
timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:!PORT!/?t=!INSTALLER_TOKEN!"
echo Der Assistent laeuft jetzt im Browser. Dieses Fenster kannst du schliessen.
goto :ende

:ohne_docker
start "" "https://www.docker.com/products/docker-desktop/"
echo Vorlesezeit braucht Docker Desktop. Installiere es von der Seite, die sich gerade geoeffnet hat, und oeffne diese Datei danach noch einmal.
goto :ende

:ohne_wsl
start "" "https://learn.microsoft.com/de-de/windows/wsl/install"
echo Docker Desktop braucht WSL2. Die Anleitung hat sich gerade geoeffnet. Danach diese Datei noch einmal oeffnen.
goto :ende

:ohne_token
echo Der Zufallsschluessel fuer den Assistenten liess sich nicht erzeugen. PowerShell fehlt oder ist gesperrt.
goto :ende

:ohne_image
echo Der Assistent liess sich nicht herunterladen. Pruef die Internetverbindung und versuch es noch einmal.
goto :ende

:startfehler
echo Der Assistent startet nicht:
type "%TEMP%\vorlesezeit-start.txt"

:ende
echo.
pause
endlocal
