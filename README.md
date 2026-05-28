# ComponentDB

ComponentDB è un'applicazione web da far girare in locale (su PC o Raspberry Pi) per gestire l'inventario dei componenti elettronici nel tuo laboratorio. 

L'idea principale è semplificare il caricamento e lo scarico dei componenti: invece di compilare fogli Excel a mano, puoi usare la fotocamera dello smartphone per scansionare direttamente i codici a barre sulle bustine (soprattutto LCSC e DigiKey) e aggiornare il database in tempo reale.

## Funzionalità principali

*   **Scanner Barcode Integrato:** Funziona direttamente dal browser del telefono. Riconosce i codici LCSC (compresi quelli strani formattati come oggetti JS senza virgolette), DigiKey e Mouser.
*   **Gestione Inventario & Storico:** Classico CRUD per i componenti, con un log immutabile (Event Sourcing) di tutte le transazioni (aggiunte, rimozioni, fix manuali).
*   **BOM Checker:** Trascina un file Excel (es. esportato da EasyEDA) nella tab dedicata. L'app incrocia la BOM con il tuo inventario locale e ti dice cosa hai in casa e cosa devi ordinare.
*   **Console SQL Integrata:** Una tab per lanciare query SQL raw direttamente sul database per estrarre statistiche o fare diagnostica.
*   **Zero configurazioni di rete:** All'avvio, lo script capisce qual è l'IP locale della macchina e genera **automaticamente un certificato SSL autofirmato**. Questo è fondamentale perché i browser moderni bloccano l'accesso alla fotocamera se non si è in HTTPS.

## Stack Tecnologico

L'app è pensata per essere leggerissima e facile da backuppare.

*   **Backend:** Python con Flask 3.0+
*   **Database:** SQLite3 in modalità **WAL** (Write-Ahead Logging). Questo permette a più dispositivi (es. tu dal PC e un collega dal telefono) di leggere e scrivere contemporaneamente senza incappare nell'errore `database is locked`. Il DB è tutto in un singolo file `components.db`.
*   **Frontend:** Vanilla HTML/CSS/JS (nessun framework pesante come React o Vue). È una Single Page Application (SPA) contenuta interamente in `templates/index.html`. 
*   **Librerie Client:** `html5-qrcode` per la fotocamera e `xlsx` per leggere le distinte base.

## Installazione e Avvio

Dato che l'app usa dipendenze specifiche (come `cryptography >= 42.0.0` per i certificati SSL moderni), è **caldamente consigliato** usare un ambiente virtuale (venv) per non sporcare il sistema operativo.

### 1. Clona la repo e crea il venv
```bash
git clone https://github.com/TUO-NOME/ComponentDB.git
cd ComponentDB

# Crea l'ambiente virtuale
python3 -m venv venv
```

### 2. Attiva il venv
*   **Su Linux/macOS:**
    ```bash
    source venv/bin/activate
    ```
*   **Su Windows (Command Prompt):**
    ```cmd
    venv\Scripts\activate.bat
    ```
*   **Su Windows (PowerShell):**
    ```powershell
    venv\Scripts\Activate.ps1
    ```

### 3. Installa i requisiti e avvia
```bash
pip install -r requirements.txt
python app.py
```

##Come usarlo

Una volta avviato `app.py`, nel terminale vedrai un output simile a questo:

```text
╔══════════════════════════════════════════════════╗
║        ComponentDB — Parts Inventory             ║
╠══════════════════════════════════════════════════╣
║  PC     → https://localhost:5000                 ║
║  Phone  → https://192.168.1.55:5000              ║
╚══════════════════════════════════════════════════╝
```

1. Dal PC, apri `https://localhost:5000`.
2. Dal telefono, assicurati di essere sotto lo stesso Wi-Fi e digita l'indirizzo IP mostrato nel terminale (es. `https://192.168.1.55:5000`).
3. **Attenzione:** La prima volta che apri la pagina, il browser ti darà un avviso di sicurezza dicendo che il certificato non è attendibile. È normale (lo abbiamo appena generato in locale). Clicca su "Avanzate" -> "Procedi comunque".

## Struttura dei file

I file che verranno generati automaticamente al primo avvio (e che sono ignorati da Git) sono:
*   `components.db`: Il tuo database vero e proprio. Fai un backup di questo file ogni tanto!
*   `cert.pem` e `key.pem`: Le chiavi per la connessione HTTPS sicura in rete locale.

### Appunti di sviluppo / Troubleshooting

*   **Fotocamera su PC:** Il lettore barcode è disabilitato via CSS/JS su Desktop per evitare casini con le webcam. Usa il telefono per scansionare, oppure la barra di input manuale su PC.
*   **Query distruttive:** Di default la tab SQL permette solo `SELECT`. Se devi fare un `UPDATE` manuale o correggere un errore, spunta il toggle "Allow writes" nell'interfaccia.
