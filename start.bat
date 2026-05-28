@echo off
cd /d "%~dp0"

echo ========================================
echo   ComponentDB - Avvio in corso (Windows)
echo ========================================

REM Crea il virtual environment se non esiste
if not exist venv\ (
    echo - Creazione del virtual environment in corso...
    python -m venv venv
)

REM Attiva il virtual environment
call venv\Scripts\activate

REM Imposta la variabile a 0 (nessuna installazione richiesta)
set "INSTALL_DEPS=0"

REM Se manca anche solo uno dei pacchetti, imposta la variabile a 1
if not exist "venv\Lib\site-packages\flask\" set "INSTALL_DEPS=1"
if not exist "venv\Lib\site-packages\cryptography\" set "INSTALL_DEPS=1"

REM Installa solo se la variabile è 1
if "%INSTALL_DEPS%"=="1" (
    echo - Installazione delle dipendenze in corso...
    pip install -q -r requirements.txt
)

REM Questo blocco viene eseguito sempre alla fine, sia che abbia installato o meno
echo - Avvio del server...
python app.py

pause