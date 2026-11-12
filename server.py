import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "mermarkids.sqlite3"
PORT = int(os.environ.get("PORT", "80"))

ACCOUNTS = ["MARIAN MP", "MARIAN EF", "MER MP", "MER EF"]
EXPENSE_ACCOUNTS = ["MER", "MARIAN", "COMPARTIDO"]
ORDER_STATUSES = ["GRAFICA", "ENTREGA", "PENDIENTE DE ENTREGA", "EN PAÑO"]
PAID_STATUSES = ["partial", "paid", "pending"]
FIRST_ORDER_NUMBER = 56789

# Solo estos archivos se sirven como estaticos (antes se exponia toda la carpeta,
# incluido server.py, Dockerfile y la base de datos si estaba dentro del proyecto).
STATIC_FILES = {"/": "index.html", "/index.html": "index.html"}
STATIC_DIRS = ("/assets/",)
MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@contextmanager
def db():
    """Abre la base, confirma o revierte la transaccion y SIEMPRE cierra la conexion."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_number INTEGER NOT NULL UNIQUE,
              date TEXT NOT NULL,
              customer TEXT NOT NULL,
              phone TEXT NOT NULL DEFAULT '',
              product TEXT NOT NULL,
              quantity REAL NOT NULL DEFAULT 0,
              unit_price REAL NOT NULL DEFAULT 0,
              total REAL NOT NULL DEFAULT 0,
              deposit REAL NOT NULL DEFAULT 0,
              account TEXT NOT NULL,
              pending REAL NOT NULL DEFAULT 0,
              paid_status TEXT NOT NULL DEFAULT 'partial',
              order_status TEXT NOT NULL DEFAULT 'PENDIENTE DE ENTREGA',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              date TEXT NOT NULL,
              concept TEXT NOT NULL,
              cash REAL NOT NULL DEFAULT 0,
              mp REAL NOT NULL DEFAULT 0,
              account TEXT NOT NULL,
              mer REAL NOT NULL DEFAULT 0,
              marian REAL NOT NULL DEFAULT 0,
              total REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_payments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER NOT NULL,
              date TEXT NOT NULL,
              account TEXT NOT NULL,
              amount REAL NOT NULL DEFAULT 0,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_order_payments_order ON order_payments(order_id);
            """
        )
        conn.execute(
            """
            INSERT INTO order_payments (order_id, date, account, amount, note, created_at)
            SELECT id, date, account, deposit, 'Seña inicial', created_at
            FROM orders
            WHERE deposit > 0
              AND NOT EXISTS (
                SELECT 1 FROM order_payments WHERE order_payments.order_id = orders.id
              )
            """
        )
        for row in conn.execute("SELECT id FROM orders").fetchall():
            sync_order_amounts(conn, row["id"], touch=False)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def as_float(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def valid_date(value):
    value = (value or "").strip()
    if not value:
        return date.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ApiError("La fecha no es valida")


def normalize_order(payload, existing=None, current_paid=0):
    quantity = as_float(payload.get("quantity"))
    unit_price = as_float(payload.get("unitPrice", payload.get("unit_price")))
    if quantity < 0 or unit_price < 0:
        raise ApiError("Cantidad y precio no pueden ser negativos")
    total = round(quantity * unit_price, 2)
    paid_status = payload.get("paidStatus", payload.get("paid_status", "partial"))
    if paid_status not in PAID_STATUSES:
        paid_status = "partial"
    payment = as_float(payload.get("deposit"))
    if paid_status == "paid":
        payment = max(total - current_paid, 0)
    if paid_status == "pending":
        payment = 0
    payment = round(min(max(payment, 0), max(total - current_paid, 0)), 2)
    deposit = round(min(current_paid + payment, total), 2)
    pending = round(max(total - deposit, 0), 2)
    if pending <= 0:
        paid_status = "paid"
    elif deposit <= 0:
        paid_status = "pending"
    else:
        paid_status = "partial"

    order_number = payload.get("orderNumber", payload.get("order_number"))
    if not order_number and existing:
        order_number = existing["order_number"]
    try:
        order_number = int(order_number)
    except (TypeError, ValueError):
        raise ApiError("El numero de orden no es valido")
    if order_number <= 0:
        raise ApiError("El numero de orden no es valido")

    customer = (payload.get("customer") or "").strip()
    product = (payload.get("product") or "").strip()
    if not customer:
        raise ApiError("Falta el nombre del cliente")
    if not product:
        raise ApiError("Falta el producto")

    account = payload.get("account") or (existing["account"] if existing else "MARIAN MP")
    if account not in ACCOUNTS:
        raise ApiError("La cuenta no es valida")

    order_status = payload.get("orderStatus", payload.get("order_status")) or "PENDIENTE DE ENTREGA"
    if order_status not in ORDER_STATUSES:
        order_status = "PENDIENTE DE ENTREGA"

    return {
        "order_number": order_number,
        "date": valid_date(payload.get("date") or payload.get("orderDate")),
        "customer": customer,
        "phone": (payload.get("phone") or "").strip(),
        "product": product,
        "quantity": quantity,
        "unit_price": unit_price,
        "total": total,
        "deposit": deposit,
        "account": account,
        "payment": payment,
        "pending": pending,
        "paid_status": paid_status,
        "order_status": order_status,
    }


def normalize_expense(payload):
    cash = as_float(payload.get("cash"))
    mp = as_float(payload.get("mp"))
    if cash < 0 or mp < 0:
        raise ApiError("Los importes no pueden ser negativos")
    total = round(cash + mp, 2)
    if total <= 0:
        raise ApiError("Cargá un importe en efectivo o MP")
    concept = (payload.get("concept") or "").strip()
    if not concept:
        raise ApiError("Falta el concepto del gasto")
    account = payload.get("account") or "MER"
    if account not in EXPENSE_ACCOUNTS:
        raise ApiError("La cuenta no es valida")
    mer = total if account == "MER" else total / 2 if account == "COMPARTIDO" else 0
    marian = total if account == "MARIAN" else total / 2 if account == "COMPARTIDO" else 0
    return {
        "date": valid_date(payload.get("date")),
        "concept": concept,
        "cash": cash,
        "mp": mp,
        "account": account,
        "mer": round(mer, 2),
        "marian": round(marian, 2),
        "total": total,
    }


def summarize_payments(payments):
    by_account = {account: 0 for account in ACCOUNTS}
    cash = mp = total = 0
    for payment in payments:
        amount = as_float(payment["amount"])
        account = payment["account"]
        by_account[account] = by_account.get(account, 0) + amount
        if "MP" in account:
            mp += amount
        else:
            cash += amount
        total += amount
    return {
        "payments": payments,
        "paymentsByAccount": {key: round(value, 2) for key, value in by_account.items()},
        "depositCash": round(cash, 2),
        "depositMp": round(mp, 2),
        "depositTotal": round(total, 2),
    }


def payment_summary(conn, order_id):
    payments = [
        dict(payment)
        for payment in conn.execute(
            "SELECT id, date, account, amount, note FROM order_payments WHERE order_id = ? ORDER BY id",
            (order_id,),
        )
    ]
    return summarize_payments(payments)


def sync_order_amounts(conn, order_id, touch=True):
    row = conn.execute("SELECT total, deposit, pending, paid_status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return
    summary = payment_summary(conn, order_id)
    deposit = round(min(summary["depositTotal"], as_float(row["total"])), 2)
    pending = round(max(as_float(row["total"]) - deposit, 0), 2)
    paid_status = "paid" if pending <= 0 else "partial" if deposit > 0 else "pending"
    unchanged = (
        as_float(row["deposit"]) == deposit
        and as_float(row["pending"]) == pending
        and row["paid_status"] == paid_status
    )
    if unchanged and not touch:
        return
    conn.execute(
        "UPDATE orders SET deposit = ?, pending = ?, paid_status = ?, updated_at = ? WHERE id = ?",
        (deposit, pending, paid_status, now_iso(), order_id),
    )


def add_payment(conn, order_id, order_date, account, amount):
    amount = round(as_float(amount), 2)
    if amount <= 0:
        return
    conn.execute(
        """
        INSERT INTO order_payments (order_id, date, account, amount, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (order_id, order_date or date.today().isoformat(), account, amount, "Seña / cobro", now_iso()),
    )


def order_row(row, conn=None, payments=None):
    item = dict(row)
    item["orderNumber"] = item.pop("order_number")
    item["unitPrice"] = item.pop("unit_price")
    item["paidStatus"] = item.pop("paid_status")
    item["orderStatus"] = item.pop("order_status")
    if payments is not None:
        item.update(summarize_payments(payments))
    elif conn:
        item.update(payment_summary(conn, item["id"]))
    else:
        item["payments"] = []
        item["paymentsByAccount"] = {}
        item["depositCash"] = item["deposit"] if "EF" in item.get("account", "") else 0
        item["depositMp"] = item["deposit"] if "MP" in item.get("account", "") else 0
        item["depositTotal"] = item["deposit"]
    return item


def load_all(conn):
    # Una sola consulta de pagos para todos los pedidos (antes era una por pedido).
    payments_by_order = {}
    for payment in conn.execute("SELECT id, order_id, date, account, amount, note FROM order_payments ORDER BY id"):
        data = dict(payment)
        order_id = data.pop("order_id")
        payments_by_order.setdefault(order_id, []).append(data)
    orders = [
        order_row(row, payments=payments_by_order.get(row["id"], []))
        for row in conn.execute("SELECT * FROM orders ORDER BY order_number DESC")
    ]
    expenses = [dict(row) for row in conn.execute("SELECT * FROM expenses ORDER BY date DESC, id DESC")]
    return {"orders": orders, "expenses": expenses}


def path_id(path):
    try:
        return int(path.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        raise ApiError("Identificador no valido", 404)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format, *args):
        return

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ApiError("Pedido invalido")
        if length == 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("Pedido demasiado grande", 413)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("Los datos enviados no son validos")
        if not isinstance(data, dict):
            raise ApiError("Los datos enviados no son validos")
        return data

    def handle_api(self, method):
        handler = getattr(self, f"api_{method}")
        try:
            handler(urlparse(self.path))
        except ApiError as error:
            self.send_json({"error": error.message}, error.status)
        except sqlite3.IntegrityError:
            self.send_json({"error": "Ya existe un pedido con ese numero"}, 409)
        except Exception as error:  # noqa: BLE001 - nunca dejar al navegador sin respuesta
            print(f"Error inesperado en {method} {self.path}: {error!r}", flush=True)
            self.send_json({"error": "Error interno del servidor"}, 500)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api("get")
            return
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        super().do_GET()

    def send_head(self):
        # Solo se sirven index.html y la carpeta assets/ (antes se exponia todo el proyecto).
        path = urlparse(self.path).path
        if path in STATIC_FILES:
            self.path = "/" + STATIC_FILES[path]
            path = self.path
        elif not path.startswith(STATIC_DIRS) or ".." in path:
            self.send_error(404, "No encontrado")
            return None
        # index.html sin cache para que siempre se vea la ultima version al actualizar el sitio
        self._cache = "no-cache" if path.endswith(".html") else "public, max-age=86400"
        return super().send_head()

    def send_response(self, code, message=None):
        super().send_response(code, message)
        cache = getattr(self, "_cache", None)
        if cache and code == 200:
            self.send_header("Cache-Control", cache)
            self._cache = None

    def api_get(self, parsed):
        if parsed.path == "/api/data":
            with db() as conn:
                payload = load_all(conn)
            self.send_json(payload)
            return

        if parsed.path == "/api/orders/next":
            with db() as conn:
                value = conn.execute(
                    "SELECT COALESCE(MAX(order_number), ?) + 1 AS next FROM orders", (FIRST_ORDER_NUMBER - 1,)
                ).fetchone()["next"]
            self.send_json({"next": value})
            return

        if parsed.path == "/api/orders/find":
            params = parse_qs(parsed.query)
            order_number = params.get("orderNumber", [""])[0].strip()
            with db() as conn:
                row = conn.execute("SELECT * FROM orders WHERE order_number = ?", (order_number,)).fetchone()
                if not row:
                    raise ApiError("Pedido no encontrado", 404)
                item = order_row(row, conn)
            self.send_json(item)
            return

        raise ApiError("Ruta no encontrada", 404)

    # ---------- POST ----------
    def do_POST(self):
        self.handle_api("post")

    def api_post(self, parsed):
        if parsed.path == "/api/orders":
            payload = normalize_order(self.read_json())
            stamp = now_iso()
            with db() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO orders (
                      order_number, date, customer, phone, product, quantity, unit_price, total,
                      deposit, account, pending, paid_status, order_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["order_number"], payload["date"], payload["customer"], payload["phone"],
                        payload["product"], payload["quantity"], payload["unit_price"], payload["total"],
                        payload["deposit"], payload["account"], payload["pending"], payload["paid_status"],
                        payload["order_status"], stamp, stamp,
                    ),
                )
                order_id = cur.lastrowid
                add_payment(conn, order_id, payload["date"], payload["account"], payload["payment"])
                sync_order_amounts(conn, order_id)
                row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                item = order_row(row, conn)
            self.send_json(item, 201)
            return

        if parsed.path == "/api/expenses":
            payload = normalize_expense(self.read_json())
            with db() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO expenses (date, concept, cash, mp, account, mer, marian, total, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["date"], payload["concept"], payload["cash"], payload["mp"], payload["account"],
                        payload["mer"], payload["marian"], payload["total"], now_iso(),
                    ),
                )
                row = conn.execute("SELECT * FROM expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
                item = dict(row)
            self.send_json(item, 201)
            return

        raise ApiError("Ruta no encontrada", 404)

    # ---------- PUT ----------
    def do_PUT(self):
        self.handle_api("put")

    def api_put(self, parsed):
        if parsed.path.startswith("/api/orders/"):
            order_id = path_id(parsed.path)
            data = self.read_json()
            with db() as conn:
                existing = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                if not existing:
                    raise ApiError("Pedido no encontrado", 404)
                current_paid = payment_summary(conn, order_id)["depositTotal"]
                payload = normalize_order(data, existing, current_paid)
                conn.execute(
                    """
                    UPDATE orders
                    SET order_number = ?, date = ?, customer = ?, phone = ?, product = ?, quantity = ?,
                        unit_price = ?, total = ?, deposit = ?, account = ?, pending = ?, paid_status = ?,
                        order_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload["order_number"], payload["date"], payload["customer"], payload["phone"],
                        payload["product"], payload["quantity"], payload["unit_price"], payload["total"],
                        payload["deposit"], payload["account"], payload["pending"], payload["paid_status"],
                        payload["order_status"], now_iso(), order_id,
                    ),
                )
                add_payment(conn, order_id, payload["date"], payload["account"], payload["payment"])
                sync_order_amounts(conn, order_id)
                row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
                item = order_row(row, conn)
            self.send_json(item)
            return

        raise ApiError("Ruta no encontrada", 404)

    # ---------- DELETE ----------
    def do_DELETE(self):
        self.handle_api("delete")

    def api_delete(self, parsed):
        if parsed.path.startswith("/api/orders/"):
            order_id = path_id(parsed.path)
            with db() as conn:
                conn.execute("DELETE FROM order_payments WHERE order_id = ?", (order_id,))
                conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
            self.send_json({"ok": True})
            return

        if parsed.path.startswith("/api/payments/"):
            # Nuevo: permite corregir una seña/cobro cargado por error sin borrar el pedido.
            payment_id = path_id(parsed.path)
            with db() as conn:
                row = conn.execute("SELECT order_id FROM order_payments WHERE id = ?", (payment_id,)).fetchone()
                if not row:
                    raise ApiError("Pago no encontrado", 404)
                conn.execute("DELETE FROM order_payments WHERE id = ?", (payment_id,))
                sync_order_amounts(conn, row["order_id"])
                order = conn.execute("SELECT * FROM orders WHERE id = ?", (row["order_id"],)).fetchone()
                item = order_row(order, conn)
            self.send_json(item)
            return

        if parsed.path.startswith("/api/expenses/"):
            expense_id = path_id(parsed.path)
            with db() as conn:
                conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            self.send_json({"ok": True})
            return

        raise ApiError("Ruta no encontrada", 404)


if __name__ == "__main__":
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"MerMarKids escuchando en puerto {PORT}", flush=True)
    server.serve_forever()
