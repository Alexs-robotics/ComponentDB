#!/usr/bin/env python3
"""
ComponentDB - Local Network Electronic Component Inventory
LCSC barcode scanning via phone/PC browser over HTTPS.
"""

from flask import Flask, request, jsonify, render_template, redirect
import sqlite3, json, re, os, socket, time
from datetime import datetime
from pathlib import Path
import urllib.request, urllib.error

#  Config 
BASE_DIR = Path(__file__).parent.absolute()
DB_PATH  = BASE_DIR / "components.db"
CERT_PATH = BASE_DIR / "cert.pem"
KEY_PATH  = BASE_DIR / "key.pem"
PORT = 5000
HOST = "0.0.0.0"

app = Flask(__name__)

#  Database 
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS components (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                part_number      TEXT UNIQUE NOT NULL,
                manufacturer_part TEXT DEFAULT '',
                description      TEXT DEFAULT '',
                supplier         TEXT DEFAULT 'Unknown',
                quantity         INTEGER DEFAULT 0,
                package          TEXT DEFAULT '',
                value            TEXT DEFAULT '',
                category         TEXT DEFAULT '',
                datasheet_url    TEXT DEFAULT '',
                notes            TEXT DEFAULT '',
                created_at       TEXT DEFAULT (datetime('now','localtime')),
                updated_at       TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id     INTEGER NOT NULL,
                part_number      TEXT NOT NULL,
                quantity_change  INTEGER NOT NULL,
                quantity_before  INTEGER NOT NULL,
                quantity_after   INTEGER NOT NULL,
                operation        TEXT NOT NULL,
                source           TEXT DEFAULT 'scan',
                barcode_raw      TEXT DEFAULT '',
                notes            TEXT DEFAULT '',
                timestamp        TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (component_id) REFERENCES components(id)
            );

            CREATE INDEX IF NOT EXISTS idx_comp_pn       ON components(part_number);
            CREATE INDEX IF NOT EXISTS idx_comp_supplier  ON components(supplier);
            CREATE INDEX IF NOT EXISTS idx_tx_component   ON transactions(component_id);
            CREATE INDEX IF NOT EXISTS idx_tx_timestamp   ON transactions(timestamp);
        """)

#  Barcode Parser 
def parse_lcsc_js_object(raw: str) -> dict:
    """
    Parse LCSC's unquoted JS-object barcode format (NOT valid JSON).
    Real example: {pbn:PICK2503110149,on:GB2503110804,pc:C492427,pm:PZ254V-12-20P,qty:10,mc:,cc:1}
      pc  = LCSC part number  (e.g. C492427)
      pm  = manufacturer part number
      qty = quantity in bag
    """
    result = {}
    inner = raw.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    for part in re.split(r",(?=\w+:)", inner):
        if ":" in part:
            key, _, val = part.partition(":")
            result[key.strip()] = val.strip()
    return result


def parse_barcode(raw: str) -> dict:
    """
    Parse LCSC barcodes. Returns structured dict.

    LCSC formats:
      - Unquoted object: {pbn:...,pc:C492427,pm:PZ254V-12-20P,qty:10,...}
      - Quoted JSON:     {"pc":"C492427","pm":"PZ254V","qty":10,...}
      - Plain text:      C123456
      - Multiline:       C123456\\n100\\nSO2024xxx
    """
    result = {
        "supplier": "LCSC",
        "part_number": None,
        "manufacturer_part": "",
        "description": "",
        "quantity": 1,
        "package": "",
        "raw": raw,
        "confidence": "low",
        "error": None,
    }

    raw = raw.strip()
    if not raw:
        result["error"] = "Empty barcode"
        return result

    # ── LCSC object / JSON ─────────────────────────────────────────────────
    if raw.startswith("{"):
        data = {}
        try:
            data = json.loads(raw)          # standard quoted JSON
        except (json.JSONDecodeError, ValueError):
            data = parse_lcsc_js_object(raw)  # unquoted JS object (real LCSC format)

        if data:
            pc  = data.get("pc", "")
            pm  = data.get("pm", "")
            qty_raw = data.get("qty", data.get("quantity", "1"))

            # pc is always the LCSC PN (C-prefixed) in modern bags
            lcsc_pn = pc if re.match(r"^C\d{3,}", pc) else ""
            mfr_pn  = pm

            # Older bag format: pm held the LCSC PN
            if not lcsc_pn and re.match(r"^C\d{3,}", pm):
                lcsc_pn, mfr_pn = pm, data.get("mpn", "")

            # Last-resort fallback field names
            if not lcsc_pn:
                lcsc_pn = data.get("partNumber") or data.get("part_number") or data.get("barcode") or ""

            if lcsc_pn:
                try:
                    qty = max(1, int(qty_raw))
                except (ValueError, TypeError):
                    qty = 1
                result.update({
                    "part_number": str(lcsc_pn).strip(),
                    "manufacturer_part": str(mfr_pn).strip(),
                    "quantity": qty,
                    "description": str(data.get("pn", data.get("description", ""))).strip(),
                    "confidence": "high",
                })
                return result

    # ── LCSC plain part number (C followed by 4-8 digits) ──────────────────
    if re.match(r"^C\d{4,8}$", raw):
        result.update({"part_number": raw, "confidence": "high"})
        return result

    # ── LCSC multiline  (PN\\nQTY\\nOrderNumber) ───────────────────────────
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    if lines and re.match(r"^C\d{4,8}$", lines[0]):
        qty = 1
        if len(lines) >= 2:
            try:
                qty = max(1, int(lines[1]))
            except ValueError:
                pass
        result.update({"part_number": lines[0], "quantity": qty, "confidence": "high"})
        return result

    # ── Unknown / unrecognised ─────────────────────────────────────────────
    result["supplier"] = "Unknown"
    result["part_number"] = raw[:120]
    return result

# In-process cache so a part is only fetched once per server run.
_LCSC_CACHE = {}

# Set LCSC_DEBUG=1 in the environment to print raw responses when a fetch fails.
_LCSC_DEBUG = os.environ.get("LCSC_DEBUG") == "1"

_BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.lcsc.com/',
    'Connection': 'close',
}


def _http_json(url, timeout=8):
    """GET a URL and parse JSON. Raises on any network/parse failure."""
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'replace')
    return json.loads(body)


def _parse_wmsc(data):
    """LCSC internal product API -> normalized fields, or None."""
    if data.get("code") == 200 and data.get("result"):
        res = data["result"]
        return {
            "manufacturer_part": res.get("productModel") or "",
            "description": res.get("productIntroEn") or "",
            "package": res.get("encapStandard") or "",
            "datasheet_url": res.get("pdfUrl") or "",
            "category": res.get("catalogName") or "",
            "value": res.get("productValue") or res.get("value") or "",
        }
    return None


def _parse_easyeda(data):
    """EasyEDA/JLCPCB component API -> normalized fields, or None.
    Field names vary by part; parsing is defensive on purpose. If a part
    comes back with empty fields, run with LCSC_DEBUG=1 to inspect the raw
    JSON and adjust the key names below."""
    if not data.get("success"):
        return None
    res = data.get("result") or {}
    head = (res.get("dataStr") or {}).get("head") or {}
    para = head.get("c_para") or {}
    return {
        "manufacturer_part": (para.get("Manufacturer Part") or para.get("Supplier Part")
                              or res.get("title") or ""),
        "description": res.get("description") or res.get("title") or "",
        "package": para.get("package") or para.get("Package") or "",
        "datasheet_url": (res.get("lcsc") or {}).get("url") or res.get("datasheet") or "",
        "category": (res.get("tags") or [""])[0] if res.get("tags") else "",
        "value": para.get("Value") or para.get("value") or "",
    }


def _fetch_lcsc(part_number):
    """Fetch part data, trying multiple endpoints with retries.

    Returns (data_dict, error_string). On success error_string is None;
    on failure data_dict is None and error_string explains why. This lets
    callers report exactly what happened instead of guessing."""
    pn = (part_number or "").strip().upper()
    if not pn.startswith("C"):
        return None, "not an LCSC part number"
    if pn in _LCSC_CACHE:
        return _LCSC_CACHE[pn], None

    sources = [
        ("wmsc", f"https://wmsc.lcsc.com/wmsc/product/detail?productCode={pn}", _parse_wmsc),
        ("easyeda", f"https://easyeda.com/api/products/{pn}/components?version=6.4.19.5", _parse_easyeda),
    ]
    last_err = None
    for name, url, parser in sources:
        for attempt in range(3):
            try:
                raw = _http_json(url)
                parsed = parser(raw)
                if parsed and any(v for v in parsed.values()):
                    _LCSC_CACHE[pn] = parsed
                    return parsed, None
                # Reached the server but it had nothing usable: move to next source.
                last_err = f"{name}: no usable data"
                if _LCSC_DEBUG:
                    print(f"[LCSC] {pn} {name} empty -> {str(raw)[:400]}")
                break
            except urllib.error.HTTPError as e:
                last_err = f"{name}: HTTP {e.code}"
            except urllib.error.URLError as e:
                last_err = f"{name}: {e.reason}"
            except Exception as e:
                last_err = f"{name}: {type(e).__name__}: {e}"
            time.sleep(0.5 * (attempt + 1))  # backoff before retrying this source
    return None, last_err or "unknown error"


def fetch_lcsc_data(part_number):
    """Backward-compatible wrapper: returns the data dict, or {} on failure."""
    data, _ = _fetch_lcsc(part_number)
    return data or {}

#  Routes 

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/parse", methods=["POST"])
def api_parse():
    data = request.json or {}
    raw = data.get("barcode", "").strip()
    if not raw:
        return jsonify({"error": "No barcode provided"}), 400
    return jsonify(parse_barcode(raw))

@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Process scanned barcode: parse → upsert component → record transaction."""
    data = request.json or {}
    raw = data.get("barcode", "").strip()
    qty_override = data.get("quantity")
    operation = data.get("operation", "ADD").upper()
    notes = data.get("notes", "")

    if not raw:
        return jsonify({"error": "No barcode provided"}), 400

    parsed = parse_barcode(raw)
    if not parsed["part_number"]:
        return jsonify({"error": "Could not parse barcode", "raw": raw}), 400

    # --- FETCH MISSING DATA ---
    enrich = fetch_lcsc_data(parsed["part_number"])
    if enrich:
        parsed["manufacturer_part"] = enrich.get("manufacturer_part") or parsed.get("manufacturer_part", "")
        parsed["description"] = enrich.get("description") or parsed.get("description", "")
        parsed["package"] = enrich.get("package", "")
        parsed["category"] = enrich.get("category", "")
        parsed["value"] = enrich.get("value", "")
        parsed["datasheet_url"] = enrich.get("datasheet_url", "")
    # --------------------------

    qty = int(qty_override) if qty_override is not None else int(parsed["quantity"])
    if qty <= 0:
        return jsonify({"error": "Quantity must be positive"}), 400

    qty_change = qty if operation == "ADD" else -qty

    conn = get_db()
    try:
        existing = conn.execute("SELECT * FROM components WHERE part_number = ?", (parsed["part_number"],)).fetchone()

        if existing:
            qty_before = existing["quantity"]
            qty_after = max(0, qty_before + qty_change)
            conn.execute(
                """UPDATE components SET
                   quantity = ?,
                   manufacturer_part = CASE WHEN manufacturer_part = '' THEN ? ELSE manufacturer_part END,
                   description = CASE WHEN description = '' THEN ? ELSE description END,
                   supplier = CASE WHEN supplier = 'Unknown' THEN ? ELSE supplier END,
                   package = CASE WHEN package = '' THEN ? ELSE package END,
                   category = CASE WHEN category = '' THEN ? ELSE category END,
                   value = CASE WHEN value = '' THEN ? ELSE value END,
                   datasheet_url = CASE WHEN datasheet_url = '' THEN ? ELSE datasheet_url END,
                   updated_at = datetime('now','localtime')
                   WHERE part_number = ?""",
                (qty_after, parsed.get("manufacturer_part", ""), parsed.get("description", ""),
                 parsed["supplier"], parsed.get("package", ""), parsed.get("category", ""), 
                 parsed.get("value", ""), parsed.get("datasheet_url", ""), parsed["part_number"]),
            )
            comp_id = existing["id"]
            is_new = False
        else:
            qty_before = 0
            qty_after = max(0, qty_change)
            cur = conn.execute(
                """INSERT INTO components
                   (part_number, manufacturer_part, description, supplier, quantity, package, category, value, datasheet_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (parsed["part_number"], parsed.get("manufacturer_part", ""),
                 parsed.get("description", ""), parsed["supplier"], qty_after,
                 parsed.get("package", ""), parsed.get("category", ""), 
                 parsed.get("value", ""), parsed.get("datasheet_url", "")),
            )
            comp_id = cur.lastrowid
            is_new = True
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/components/enrich", methods=["POST"])
def api_enrich_all():
    """Fill in missing data for LCSC parts that have no category yet.

    Selects on category='' only (not value=''), because many parts legitimately
    have no value and would otherwise be re-fetched on every run, inflating the
    count while changing nothing. Reports attempted / enriched / failed so the
    UI can never disguise a no-op as a success."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, part_number FROM components "
            "WHERE part_number LIKE 'C%' AND category = '' "
            "ORDER BY id LIMIT 100"
        ).fetchall()
        attempted = len(rows)
        enriched = 0
        failures = []
        for r in rows:
            data, err = _fetch_lcsc(r["part_number"])
            if err or not data:
                failures.append({"part": r["part_number"], "reason": err or "no data"})
                continue
            cur = conn.execute(
                """UPDATE components SET
                   manufacturer_part = CASE WHEN manufacturer_part='' THEN ? ELSE manufacturer_part END,
                   description       = CASE WHEN description='' THEN ? ELSE description END,
                   package           = CASE WHEN package='' THEN ? ELSE package END,
                   category          = CASE WHEN category='' THEN ? ELSE category END,
                   value             = CASE WHEN value='' THEN ? ELSE value END,
                   datasheet_url     = CASE WHEN datasheet_url='' THEN ? ELSE datasheet_url END,
                   updated_at        = datetime('now','localtime')
                   WHERE id = ? AND category = ''""",
                (data.get("manufacturer_part", ""), data.get("description", ""),
                 data.get("package", ""), data.get("category", ""),
                 data.get("value", ""), data.get("datasheet_url", ""), r["id"])
            )
            if cur.rowcount > 0:
                enriched += 1
        conn.commit()
        return jsonify({
            "success": True,
            "attempted": attempted,
            "enriched": enriched,
            "failed": len(failures),
            "failures": failures[:10],  # sample for debugging
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/components", methods=["GET"])
def api_components():
    search   = request.args.get("q", "")
    supplier = request.args.get("supplier", "")
    limit    = min(int(request.args.get("limit", 200)), 1000)
    offset   = int(request.args.get("offset", 0))
    sort     = request.args.get("sort", "updated_at")
    order    = request.args.get("order", "DESC")

    allowed_sorts  = {"id","part_number","manufacturer_part","supplier","quantity","updated_at","created_at"}
    allowed_orders = {"ASC","DESC"}
    sort  = sort  if sort  in allowed_sorts  else "updated_at"
    order = order if order in allowed_orders else "DESC"

    conn = get_db()
    try:
        q, params = "SELECT * FROM components WHERE 1=1", []
        if search:
            q += " AND (part_number LIKE ? OR manufacturer_part LIKE ? OR description LIKE ? OR value LIKE ? OR package LIKE ? OR category LIKE ?)"
            s = f"%{search}%"
            params += [s,s,s,s,s,s]
        if supplier:
            q += " AND supplier = ?"
            params.append(supplier)
        q += f" ORDER BY {sort} {order} LIMIT ? OFFSET ?"
        params += [limit, offset]

        rows  = conn.execute(q, params).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
        return jsonify({"components": [dict(r) for r in rows], "total": total})
    finally:
        conn.close()

@app.route("/api/components/<int:cid>", methods=["GET","PUT","DELETE"])
def api_component(cid):
    conn = get_db()
    try:
        if request.method == "GET":
            row = conn.execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
            return jsonify(dict(row)) if row else (jsonify({"error":"Not found"}), 404)

        if request.method == "DELETE":
            # PRIMA elimina tutte le transazioni collegate a questo componente
            conn.execute("DELETE FROM transactions WHERE component_id=?", (cid,))
            # POI elimina il componente stesso
            conn.execute("DELETE FROM components WHERE id=?", (cid,))
            conn.commit()
            return jsonify({"success": True})

        # PUT
        data = request.json or {}
        allowed = ["manufacturer_part","description","supplier","package","value","category","datasheet_url","notes"]
        updates = {k: v for k,v in data.items() if k in allowed}

        if "quantity" in data:
            row = conn.execute("SELECT quantity FROM components WHERE id=?", (cid,)).fetchone()
            if row:
                old_qty = row["quantity"]
                new_qty = max(0, int(data["quantity"]))
                delta   = new_qty - old_qty
                updates["quantity"] = new_qty
                if delta:
                    conn.execute(
                        """INSERT INTO transactions
                           (component_id, part_number, quantity_change, quantity_before, quantity_after,
                            operation, source)
                           VALUES (?, (SELECT part_number FROM components WHERE id=?),
                           ?, ?, ?, ?, 'manual')""",
                        (cid, cid, delta, old_qty, new_qty, "ADD" if delta > 0 else "REMOVE"),
                    )

        if updates:
            updates["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE components SET {clause} WHERE id=?", list(updates.values()) + [cid])
            conn.commit()

        row = conn.execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
        return jsonify(dict(row)) if row else (jsonify({"error":"Not found"}), 404)
    finally:
        conn.close()

@app.route("/api/components/<int:cid>/adjust", methods=["POST"])
def api_adjust(cid):
    data = request.json or {}
    change = int(data.get("change", 0))
    notes  = data.get("notes", "")
    if change == 0:
        return jsonify({"error": "Change cannot be zero"}), 400

    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        old_qty = row["quantity"]
        new_qty = max(0, old_qty + change)
        conn.execute("UPDATE components SET quantity=?, updated_at=datetime('now','localtime') WHERE id=?", (new_qty, cid))
        conn.execute(
            """INSERT INTO transactions
               (component_id, part_number, quantity_change, quantity_before, quantity_after,
                operation, source, notes)
               VALUES (?, ?, ?, ?, ?, ?, 'manual', ?)""",
            (cid, row["part_number"], change, old_qty, new_qty,
             "ADD" if change > 0 else "REMOVE", notes),
        )
        conn.commit()
        return jsonify({"success": True, "quantity_before": old_qty, "quantity_after": new_qty})
    finally:
        conn.close()

@app.route("/api/query", methods=["POST"])
def api_query():
    data  = request.json or {}
    sql   = data.get("sql", "").strip()
    write = data.get("allow_write", False)
    if not sql:
        return jsonify({"error": "No SQL provided"}), 400

    sql_up = sql.upper().lstrip()
    if not write and not sql_up.startswith("SELECT"):
        return jsonify({"error": "Only SELECT queries by default. Toggle 'Allow writes' to run INSERT/UPDATE/DELETE."}), 400

    conn = get_db()
    try:
        cur = conn.execute(sql)
        if sql_up.startswith("SELECT"):
            cols = [d[0] for d in (cur.description or [])]
            rows = [list(r) for r in cur.fetchmany(2000)]
            return jsonify({"columns": cols, "rows": rows, "count": len(rows)})
        conn.commit()
        return jsonify({"success": True, "rowcount": cur.rowcount, "columns": [], "rows": []})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route("/api/history", methods=["GET"])
def api_history():
    limit = int(request.args.get("limit", 100))
    cid   = request.args.get("component_id")
    conn  = get_db()
    try:
        q = """SELECT t.*, c.supplier, c.description as comp_desc
               FROM transactions t LEFT JOIN components c ON t.component_id=c.id
               WHERE 1=1"""
        params = []
        if cid:
            q += " AND t.component_id=?"
            params.append(int(cid))
        q += " ORDER BY t.timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        return jsonify({"transactions": [dict(r) for r in rows]})
    finally:
        conn.close()

@app.route("/api/stats", methods=["GET"])
def api_stats():
    conn = get_db()
    try:
        total_parts = conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]
        total_qty   = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM components").fetchone()[0]
        low_stock   = conn.execute("SELECT COUNT(*) FROM components WHERE quantity<=5 AND quantity>=0").fetchone()[0]
        out_stock   = conn.execute("SELECT COUNT(*) FROM components WHERE quantity=0").fetchone()[0]
        recent      = conn.execute("SELECT COUNT(*) FROM transactions WHERE timestamp > datetime('now','-24 hours','localtime')").fetchone()[0]
        by_supplier = [dict(r) for r in conn.execute("SELECT supplier, COUNT(*) as count, SUM(quantity) as total_qty FROM components GROUP BY supplier").fetchall()]
        return jsonify({
            "total_parts": total_parts,
            "total_quantity": int(total_qty),
            "low_stock_count": low_stock,
            "out_of_stock": out_stock,
            "recent_scans_24h": recent,
            "by_supplier": by_supplier,
        })
    finally:
        conn.close()

#  SSL 
def generate_ssl_cert():
    if CERT_PATH.exists() and KEY_PATH.exists():
        print("🔐 Using existing SSL certificate")
        return str(CERT_PATH), str(KEY_PATH)
    print("📜 Generating self-signed SSL certificate (valid 10 years)…")
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import ipaddress, datetime as dt

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ComponentDB")])

        san = [x509.DNSName("localhost")]
        for ip_str in get_all_local_ips():
            try: san.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
            except: pass

        cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.utcnow())
            .not_valid_after(dt.datetime.utcnow() + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))

        CERT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        KEY_PATH.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        print(f"✅ Certificate written to {CERT_PATH}")
        return str(CERT_PATH), str(KEY_PATH)
    except ImportError:
        print("⚠️  cryptography not installed — falling back to adhoc SSL")
        return "adhoc"

def get_all_local_ips():
    ips = ["127.0.0.1"]
    try:
        hostname = socket.gethostname()
        ips += socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        ips = list({ip[4][0] for ip in ips if isinstance(ip, tuple)} | {"127.0.0.1"})
    except: pass
    return ips

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except: return "127.0.0.1"

# ─── Main
if __name__ == "__main__":
    init_db()
    local_ip  = get_local_ip()
    ssl_ctx   = generate_ssl_cert()
    ssl_context = ssl_ctx if ssl_ctx == "adhoc" else tuple(ssl_ctx)

    print(f"""
╔══════════════════════════════════════════════════╗
║        ComponentDB — Parts Inventory             ║
╠══════════════════════════════════════════════════╣
║  PC     → https://localhost:{PORT}               ║
║  Phone  → https://{local_ip:<22}:{PORT}  ║
║                                                  ║
║  ⚠  Accept the browser SSL warning once          ║
║     (self-signed cert — safe on local network)   ║
╚══════════════════════════════════════════════════╝
""")
    app.run(host=HOST, port=PORT, ssl_context=ssl_context, debug=False, threaded=True)