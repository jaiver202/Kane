"""Lógica de negocio, acceso a datos y generación de PDFs de CandyKane."""

import io
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from secrets import token_hex
from xml.sax.saxutils import escape as xml_escape

import mysql.connector
from dotenv import load_dotenv
from mysql.connector import Error
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "kane_db"),
    "connection_timeout": 5,
}


class NotFoundError(Exception):
    """El registro solicitado no existe."""


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def _close(connection):
    if connection is not None and connection.is_connected():
        connection.close()


def _rollback(connection):
    if connection is not None:
        connection.rollback()


SPANISH_MONTHS = {
    1: "ene.",
    2: "feb.",
    3: "mar.",
    4: "abr.",
    5: "may.",
    6: "jun.",
    7: "jul.",
    8: "ago.",
    9: "sep.",
    10: "oct.",
    11: "nov.",
    12: "dic.",
}


def format_date_es(value):
    if value is None:
        return ""
    return f"{value.day:02d} de {SPANISH_MONTHS[value.month]} de {value.year}"


def format_currency(amount):
    value = Decimal(str(amount))
    if value == value.to_integral_value():
        number = f"{value:,.0f}"
    else:
        number = f"{value:,.2f}"
    return "$ " + number.replace(",", "X").replace(".", ",").replace("X", ".")


def add_report_participation(report_rows):
    total = sum(row["cantidad_clientes"] for row in report_rows)
    cumulative_count = 0
    previous_percentage = 0
    for row in report_rows:
        cumulative_count += row["cantidad_clientes"]
        cumulative_percentage = round(cumulative_count * 100 / total) if total else 0
        row["participacion"] = cumulative_percentage - previous_percentage
        previous_percentage = cumulative_percentage
    return total


def parse_money(value, field_name):
    try:
        amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} debe ser un valor numérico válido.")
    if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99"):
        raise ValueError(f"{field_name} debe estar entre 0 y 99,999,999.99.")
    return amount



# Autenticación
def authenticate_user(email, password):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id_usuario, nombre_usuario, email, contrasena, rol, estado
                FROM usuarios
                WHERE email = %s
                """,
                (email,),
            )
            user = cursor.fetchone()
    finally:
        _close(connection)

    if (
        user
        and user[5] == "activo"
        and user[4] in ("administrador", "vendedor")
        and check_password_hash(user[3], password)
    ):
        return user
    return None



# Panel principal

def get_dashboard_data():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT COUNT(*) AS total_clientes FROM cliente")
            total_clientes = cursor.fetchone()["total_clientes"]

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(
                        CASE WHEN DATE(fecha_venta) = CURRENT_DATE()
                        THEN total ELSE 0 END
                    ), 0) AS ventas_hoy,
                    COUNT(
                        CASE WHEN DATE(fecha_venta) = CURRENT_DATE()
                        THEN 1 END
                    ) AS transacciones_hoy,
                    COALESCE(SUM(
                        CASE
                            WHEN YEAR(fecha_venta) = YEAR(CURRENT_DATE())
                             AND MONTH(fecha_venta) = MONTH(CURRENT_DATE())
                            THEN total ELSE 0
                        END
                    ), 0) AS ventas_mes
                FROM venta
                """
            )
            metricas_ventas = cursor.fetchone()

            cursor.execute(
                """
                SELECT v.id_venta, v.fecha_venta, v.total, c.nombre AS cliente,
                       h.id_factura, h.numero_factura
                FROM venta AS v
                JOIN cliente AS c ON c.id_cliente = v.id_cliente
                LEFT JOIN historial_facturas AS h ON h.id_venta = v.id_venta
                ORDER BY v.fecha_venta DESC, v.id_venta DESC
                LIMIT 5
                """
            )
            ventas_recientes = cursor.fetchall()

            cursor.execute(
                """
                SELECT p.id_producto, p.nombre_producto,
                       COALESCE(SUM(s.cantidad_disponible), 0) AS cantidad_disponible
                FROM producto AS p
                LEFT JOIN stock AS s ON s.id_producto = p.id_producto
                GROUP BY p.id_producto, p.nombre_producto
                HAVING cantidad_disponible <= 3
                ORDER BY cantidad_disponible, p.nombre_producto
                LIMIT 5
                """
            )
            alertas_stock = cursor.fetchall()

            cursor.execute(
                """
                SELECT COUNT(*) AS total_stock_bajo
                FROM (
                    SELECT p.id_producto,
                           COALESCE(SUM(s.cantidad_disponible), 0) AS cantidad_disponible
                    FROM producto AS p
                    LEFT JOIN stock AS s ON s.id_producto = p.id_producto
                    GROUP BY p.id_producto
                    HAVING cantidad_disponible <= 3
                ) AS productos_stock_bajo
                """
            )
            total_stock_bajo = cursor.fetchone()["total_stock_bajo"]
    finally:
        _close(connection)

    for venta in ventas_recientes:
        venta["fecha_display"] = format_date_es(venta["fecha_venta"])
        venta["total_display"] = format_currency(venta["total"])

    return {
        "total_clientes": total_clientes,
        "ventas_hoy": metricas_ventas["ventas_hoy"],
        "ventas_hoy_display": format_currency(metricas_ventas["ventas_hoy"]),
        "transacciones_hoy": metricas_ventas["transacciones_hoy"],
        "ventas_mes": metricas_ventas["ventas_mes"],
        "ventas_mes_display": format_currency(metricas_ventas["ventas_mes"]),
        "ventas_recientes": ventas_recientes,
        "alertas_stock": alertas_stock,
        "total_stock_bajo": total_stock_bajo,
    }



# Usuarios

def get_users():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_usuario, nombre_usuario, email, rol, estado
                FROM usuarios
                ORDER BY nombre_usuario, id_usuario
                """
            )
            return cursor.fetchall()
    finally:
        _close(connection)


def create_user_record(username, email, password, role, state):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO usuarios (nombre_usuario, email, contrasena, rol, estado)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, email, generate_password_hash(password), role, state),
            )
        connection.commit()
    except Error:
        _rollback(connection)
        raise
    finally:
        _close(connection)


def update_user_record(user_id, username, email, password, role, state):
    connection = None
    try:
        connection = get_connection()
        connection.start_transaction()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rol, estado
                FROM usuarios
                WHERE id_usuario = %s
                FOR UPDATE
                """,
                (user_id,),
            )
            current_user = cursor.fetchone()
            if current_user is None:
                raise NotFoundError

            if (
                current_user[0] == "administrador"
                and current_user[1] == "activo"
                and (role != "administrador" or state != "activo")
            ):
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM usuarios
                    WHERE rol = 'administrador' AND estado = 'activo'
                      AND id_usuario <> %s
                    """,
                    (user_id,),
                )
                if cursor.fetchone()[0] == 0:
                    raise ValueError("Debe permanecer al menos un administrador activo.")

            if password:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET nombre_usuario = %s, email = %s, contrasena = %s,
                        rol = %s, estado = %s
                    WHERE id_usuario = %s
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(password),
                        role,
                        state,
                        user_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE usuarios
                    SET nombre_usuario = %s, email = %s, rol = %s, estado = %s
                    WHERE id_usuario = %s
                    """,
                    (username, email, role, state, user_id),
                )
        connection.commit()
    except (Error, ValueError, NotFoundError):
        _rollback(connection)
        raise
    finally:
        _close(connection)


def get_user(user_id):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_usuario, nombre_usuario, email, rol, estado
                FROM usuarios
                WHERE id_usuario = %s
                """,
                (user_id,),
            )
            return cursor.fetchone()
    finally:
        _close(connection)


def delete_user_record(user_id):
    connection = None
    try:
        connection = get_connection()
        connection.start_transaction()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rol, estado
                FROM usuarios
                WHERE id_usuario = %s
                FOR UPDATE
                """,
                (user_id,),
            )
            user = cursor.fetchone()
            if user is None:
                raise NotFoundError

            if user[0] == "administrador" and user[1] == "activo":
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM usuarios
                    WHERE rol = 'administrador' AND estado = 'activo'
                      AND id_usuario <> %s
                    """,
                    (user_id,),
                )
                if cursor.fetchone()[0] == 0:
                    raise ValueError("Debe permanecer al menos un administrador activo.")

            cursor.execute("DELETE FROM usuarios WHERE id_usuario = %s", (user_id,))
        connection.commit()
    except (Error, ValueError, NotFoundError):
        _rollback(connection)
        raise
    finally:
        _close(connection)



# Productos y stock
def get_products():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT p.id_producto, p.nombre_producto, p.precio, p.descripcion,
                       COALESCE(SUM(s.cantidad_disponible), 0) AS cantidad_disponible,
                       COUNT(s.id_stock) AS registros_stock
                FROM producto AS p
                LEFT JOIN stock AS s ON s.id_producto = p.id_producto
                GROUP BY p.id_producto, p.nombre_producto, p.precio, p.descripcion
                ORDER BY p.nombre_producto
                """
            )
            return cursor.fetchall()
    finally:
        _close(connection)


def create_product_record(name, price, description, quantity):
    connection = None
    try:
        connection = get_connection()
        connection.start_transaction()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO producto (nombre_producto, precio, descripcion) VALUES (%s, %s, %s)",
                (name, price, description),
            )
            product_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO stock (id_producto, cantidad_disponible) VALUES (%s, %s)",
                (product_id, quantity),
            )
        connection.commit()
    except Error:
        _rollback(connection)
        raise
    finally:
        _close(connection)


def update_product_record(product_id, name, price, description, quantity=None):
    connection = None
    stock_warning = False
    try:
        connection = get_connection()
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id_producto FROM producto WHERE id_producto = %s FOR UPDATE",
                (product_id,),
            )
            if cursor.fetchone() is None:
                raise NotFoundError

            cursor.execute(
                """
                UPDATE producto
                SET nombre_producto = %s, precio = %s, descripcion = %s
                WHERE id_producto = %s
                """,
                (name, price, description, product_id),
            )

            if quantity is not None:
                cursor.execute(
                    "SELECT id_stock FROM stock WHERE id_producto = %s FOR UPDATE",
                    (product_id,),
                )
                stock_rows = cursor.fetchall()
                if len(stock_rows) == 1:
                    cursor.execute(
                        "UPDATE stock SET cantidad_disponible = %s WHERE id_stock = %s",
                        (quantity, stock_rows[0]["id_stock"]),
                    )
                elif not stock_rows:
                    cursor.execute(
                        "INSERT INTO stock (id_producto, cantidad_disponible) VALUES (%s, %s)",
                        (product_id, quantity),
                    )
                else:
                    stock_warning = True
        connection.commit()
        return stock_warning
    except (Error, NotFoundError):
        _rollback(connection)
        raise
    finally:
        _close(connection)


def get_product(product_id):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_producto, nombre_producto, precio, descripcion
                FROM producto WHERE id_producto = %s
                """,
                (product_id,),
            )
            return cursor.fetchone()
    finally:
        _close(connection)


def add_stock_record(product_id, quantity_to_add):
    connection = None
    try:
        connection = get_connection()
        connection.start_transaction()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id_stock, cantidad_disponible FROM stock WHERE id_producto = %s FOR UPDATE",
                (product_id,),
            )
            stock_rows = cursor.fetchall()
            if len(stock_rows) != 1:
                raise ValueError(
                    "No se pudo actualizar el stock: el producto no tiene un único registro de existencias."
                )
            stock_id, available = stock_rows[0]
            if available + quantity_to_add > 2147483647:
                raise ValueError("La existencia excedería el máximo permitido.")
            cursor.execute(
                "UPDATE stock SET cantidad_disponible = %s WHERE id_stock = %s",
                (available + quantity_to_add, stock_id),
            )
        connection.commit()
    except (Error, ValueError):
        _rollback(connection)
        raise
    finally:
        _close(connection)



# Ventas

def get_sales_data():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id_cliente, nombre, email FROM cliente ORDER BY nombre"
            )
            client_list = cursor.fetchall()
            cursor.execute(
                """
                SELECT p.id_producto, p.nombre_producto, p.precio, p.descripcion,
                       COALESCE(SUM(s.cantidad_disponible), 0) AS cantidad_disponible,
                       COUNT(s.id_stock) AS registros_stock
                FROM producto AS p
                LEFT JOIN stock AS s ON s.id_producto = p.id_producto
                GROUP BY p.id_producto, p.nombre_producto, p.precio, p.descripcion
                ORDER BY p.nombre_producto
                """
            )
            product_list = cursor.fetchall()
    finally:
        _close(connection)

    return client_list, product_list


def create_sale_record(client_id, user_id, requested_items):
    connection = None
    try:
        connection = get_connection()
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id_cliente FROM cliente WHERE id_cliente = %s FOR UPDATE",
                (client_id,),
            )
            if cursor.fetchone() is None:
                raise ValueError("El cliente seleccionado no existe.")

            sale_items = []
            total = Decimal("0.00")
            for product_id, quantity in requested_items.items():
                cursor.execute(
                    """
                    SELECT id_producto, nombre_producto, precio
                    FROM producto WHERE id_producto = %s FOR UPDATE
                    """,
                    (product_id,),
                )
                product = cursor.fetchone()
                if product is None:
                    raise ValueError("Uno de los productos seleccionados ya no existe.")

                cursor.execute(
                    """
                    SELECT id_stock, cantidad_disponible
                    FROM stock WHERE id_producto = %s FOR UPDATE
                    """,
                    (product_id,),
                )
                stock_rows = cursor.fetchall()
                if len(stock_rows) != 1:
                    raise ValueError(
                        f"El producto {product['nombre_producto']} no tiene un único registro de stock."
                    )
                stock_row = stock_rows[0]
                if stock_row["cantidad_disponible"] < quantity:
                    raise ValueError(
                        f"Existencias insuficientes para {product['nombre_producto']}. "
                        f"Disponibles: {stock_row['cantidad_disponible']}."
                    )

                unit_price = Decimal(str(product["precio"])).quantize(Decimal("0.01"))
                line_subtotal = (unit_price * quantity).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                total += line_subtotal
                sale_items.append(
                    {
                        "product_id": product_id,
                        "stock_id": stock_row["id_stock"],
                        "name": product["nombre_producto"],
                        "quantity": quantity,
                        "subtotal": line_subtotal,
                    }
                )

            if total > Decimal("99999999.99"):
                raise ValueError("El total de la venta excede el máximo permitido.")

            cursor.execute(
                """
                INSERT INTO venta (id_cliente, id_usuario, total)
                VALUES (%s, %s, %s)
                """,
                (client_id, user_id, total),
            )
            sale_id = cursor.lastrowid

            for item in sale_items:
                cursor.execute(
                    """
                    INSERT INTO detalle_venta (id_venta, id_producto, cantidad, subtotal)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (sale_id, item["product_id"], item["quantity"], item["subtotal"]),
                )
                cursor.execute(
                    """
                    UPDATE stock
                    SET cantidad_disponible = cantidad_disponible - %s
                    WHERE id_stock = %s AND cantidad_disponible >= %s
                    """,
                    (item["quantity"], item["stock_id"], item["quantity"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        f"Las existencias de {item['name']} cambiaron. Vuelve a intentar la venta."
                    )

            invoice_number = f"FAC-{datetime.now():%Y%m%d}-{token_hex(4).upper()}"
            cursor.execute(
                """
                INSERT INTO historial_facturas (id_venta, numero_factura)
                VALUES (%s, %s)
                """,
                (sale_id, invoice_number),
            )
            invoice_id = cursor.lastrowid

        connection.commit()
        return invoice_id
    except (Error, ValueError):
        _rollback(connection)
        raise
    finally:
        _close(connection)



# Facturas

def fetch_invoice_items(cursor, invoice_ids):
    invoice_items = {}
    if not invoice_ids:
        return invoice_items

    placeholders = ", ".join(["%s"] * len(invoice_ids))
    cursor.execute(
        f"""
        SELECT h.id_factura, p.nombre_producto, d.cantidad, d.subtotal
        FROM detalle_venta AS d
        JOIN producto AS p ON p.id_producto = d.id_producto
        JOIN venta AS v ON v.id_venta = d.id_venta
        JOIN historial_facturas AS h ON h.id_venta = v.id_venta
        WHERE h.id_factura IN ({placeholders})
        ORDER BY d.id_detalle
        """,
        invoice_ids,
    )
    for item in cursor.fetchall():
        invoice_items.setdefault(item["id_factura"], []).append(item)
    return invoice_items


def serialize_invoices(facturas, invoice_items):
    invoices_json = []
    for factura in facturas:
        factura["fecha_display"] = format_date_es(factura["fecha_emision"])
        factura["total_display"] = format_currency(factura["total"])
        factura["contacto"] = (
            factura.get("telefono_cliente") or factura.get("email_cliente") or ""
        )

        payload_items = []
        subtotal = Decimal("0.00")
        for item in invoice_items.get(factura["id_factura"], []):
            quantity = int(item["cantidad"])
            line_subtotal = Decimal(str(item["subtotal"]))
            subtotal += line_subtotal
            unit_price = (
                (line_subtotal / quantity).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                if quantity
                else Decimal("0.00")
            )
            payload_items.append(
                {
                    "nombre": item["nombre_producto"],
                    "cantidad": quantity,
                    "precio": format_currency(unit_price),
                    "subtotal": format_currency(line_subtotal),
                }
            )

        invoices_json.append(
            {
                "id": factura["id_factura"],
                "numero": factura["numero_factura"],
                "fecha": factura["fecha_display"],
                "cliente": factura["cliente"],
                "telefono": factura.get("telefono_cliente") or "",
                "email": factura.get("email_cliente") or "",
                "vendedor": factura["vendedor"],
                "subtotal": format_currency(subtotal),
                "total": factura["total_display"],
                "items": payload_items,
            }
        )

    return invoices_json


def get_invoices():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT h.id_factura, h.numero_factura, h.fecha_emision,
                       v.id_venta, v.total, c.nombre AS cliente,
                       c.email AS email_cliente, c.telefono AS telefono_cliente,
                       u.nombre_usuario AS vendedor
                FROM historial_facturas AS h
                JOIN venta AS v ON v.id_venta = h.id_venta
                JOIN cliente AS c ON c.id_cliente = v.id_cliente
                JOIN usuarios AS u ON u.id_usuario = v.id_usuario
                ORDER BY h.fecha_emision DESC, h.id_factura DESC
                LIMIT 100
                """
            )
            facturas = cursor.fetchall()
            invoice_items = fetch_invoice_items(
                cursor, [factura["id_factura"] for factura in facturas]
            )
    finally:
        _close(connection)

    return facturas, serialize_invoices(facturas, invoice_items)


def get_invoice_by_id(invoice_id):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT h.id_factura, h.numero_factura, h.fecha_emision, v.total,
                       v.id_venta, v.fecha_venta, c.nombre AS cliente,
                       c.email AS email_cliente, c.telefono AS telefono_cliente,
                       u.nombre_usuario AS vendedor
                FROM historial_facturas AS h
                JOIN venta AS v ON v.id_venta = h.id_venta
                JOIN cliente AS c ON c.id_cliente = v.id_cliente
                JOIN usuarios AS u ON u.id_usuario = v.id_usuario
                WHERE h.id_factura = %s
                """,
                (invoice_id,),
            )
            invoice_data = cursor.fetchone()
            if invoice_data is None:
                return None, []

            cursor.execute(
                """
                SELECT p.nombre_producto, d.cantidad, d.subtotal
                FROM detalle_venta AS d
                JOIN producto AS p ON p.id_producto = d.id_producto
                WHERE d.id_venta = %s
                ORDER BY d.id_detalle
                """,
                (invoice_data["id_venta"],),
            )
            items = cursor.fetchall()
    finally:
        _close(connection)

    for item in items:
        item["precio_unitario"] = (
            Decimal(str(item["subtotal"])) / item["cantidad"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return invoice_data, items



# Clientes

def get_clients():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_cliente, nombre, email, telefono, direccion, ciudad, pais,
                       fecha_registro
                FROM cliente
                ORDER BY id_cliente DESC
                """
            )
            return cursor.fetchall()
    finally:
        _close(connection)


def get_clients_page_data():
    client_list = get_clients()

    clientes_por_pais = {}
    for cliente in client_list:
        cliente["fecha_registro_texto"] = format_date_es(
            cliente["fecha_registro"]
        ).replace(".", "")
        pais = cliente["pais"]
        clientes_por_pais[pais] = clientes_por_pais.get(pais, 0) + 1

    total_clientes_reporte = len(client_list)
    report_rows = (
        [
            {
                "pais": pais,
                "cantidad_clientes": cantidad,
            }
            for pais, cantidad in sorted(
                clientes_por_pais.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )
        ]
        if total_clientes_reporte
        else []
    )
    add_report_participation(report_rows)

    return client_list, report_rows, total_clientes_reporte


def create_client_record(nombre, pais, email, telefono, direccion, ciudad):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO cliente (nombre, email, telefono, direccion, ciudad, pais)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (nombre, email, telefono, direccion, ciudad, pais),
            )
        connection.commit()
    except Error:
        _rollback(connection)
        raise
    finally:
        _close(connection)


def update_client_record(client_id, nombre, pais, email, telefono, direccion, ciudad):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE cliente
                SET nombre = %s, email = %s, telefono = %s, direccion = %s,
                    ciudad = %s, pais = %s
                WHERE id_cliente = %s
                """,
                (nombre, email, telefono, direccion, ciudad, pais, client_id),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT id_cliente FROM cliente WHERE id_cliente = %s",
                    (client_id,),
                )
                if cursor.fetchone() is None:
                    raise NotFoundError
        connection.commit()
    except (Error, NotFoundError):
        _rollback(connection)
        raise
    finally:
        _close(connection)


def get_client(client_id):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_cliente, nombre, email, telefono, direccion, ciudad, pais
                FROM cliente
                WHERE id_cliente = %s
                """,
                (client_id,),
            )
            return cursor.fetchone()
    finally:
        _close(connection)


def delete_client_record(client_id):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM cliente WHERE id_cliente = %s", (client_id,))
            if cursor.rowcount == 0:
                raise NotFoundError
        connection.commit()
    except (Error, NotFoundError):
        _rollback(connection)
        raise
    finally:
        _close(connection)


def get_client_report_data():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT pais, COUNT(*) AS cantidad_clientes
                FROM cliente
                GROUP BY pais
                ORDER BY cantidad_clientes DESC, pais ASC
                """
            )
            report_rows = cursor.fetchall()
            total_clientes = add_report_participation(report_rows)
    finally:
        _close(connection)

    return report_rows, total_clientes



# Administración y diagnóstico


def create_admin_record(username, email, password):
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT id_usuario FROM usuarios WHERE email = %s", (email,))
            if cursor.fetchone():
                raise ValueError("Ya existe un usuario con ese correo.")
            cursor.execute(
                """
                INSERT INTO usuarios (nombre_usuario, email, contrasena, rol, estado)
                VALUES (%s, %s, %s, 'administrador', 'activo')
                """,
                (username, email, generate_password_hash(password)),
            )
        connection.commit()
    except (Error, ValueError):
        _rollback(connection)
        raise
    finally:
        _close(connection)


def get_table_names():
    connection = None
    try:
        connection = get_connection()
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            return [row[0] for row in cursor.fetchall()]
    finally:
        _close(connection)


# PDFs
def build_client_report_pdf(report_rows, total_clientes, report_date):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title="Reporte de clientes por nacionalidad",
        author="CandyKane",
        subject="Reporte CRM de clientes",
    )

    ink = colors.HexColor("#2A121D")
    muted = colors.HexColor("#7D6671")
    pink = colors.HexColor("#A83E69")
    pink_soft = colors.HexColor("#F9E9EF")
    line = colors.HexColor("#EDDEE4")

    brand_style = ParagraphStyle(
        "ReportBrand", fontName="Times-Bold", fontSize=22, leading=24, textColor=pink
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=muted,
    )
    date_style = ParagraphStyle(
        "ReportDate",
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=muted,
        alignment=TA_RIGHT,
    )
    title_style = ParagraphStyle(
        "ReportTitle", fontName="Times-Bold", fontSize=17, leading=21, textColor=ink
    )
    header_cell_style = ParagraphStyle(
        "ReportHeaderCell",
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=10,
        textColor=pink,
    )
    cell_style = ParagraphStyle(
        "ReportCell", fontName="Helvetica", fontSize=9, leading=12, textColor=ink
    )
    total_label_style = ParagraphStyle(
        "ReportTotalLabel",
        fontName="Helvetica-Bold",
        fontSize=9.5,
        leading=12,
        textColor=ink,
    )
    empty_style = ParagraphStyle(
        "ReportEmpty",
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=muted,
        alignment=TA_CENTER,
    )

    story = []
    brand_cell = [
        Paragraph("CandyKane", brand_style),
        Spacer(1, 3),
        Paragraph("Reporte CRM de clientes", subtitle_style),
    ]
    date_cell = [Spacer(1, 9), Paragraph(xml_escape(report_date), date_style)]
    header = Table(
        [[brand_cell, date_cell]], colWidths=[doc.width * 0.7, doc.width * 0.3]
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 10))
    story.append(
        HRFlowable(width="100%", thickness=0.8, color=line, spaceBefore=0, spaceAfter=0)
    )
    story.append(Spacer(1, 16))
    story.append(Paragraph("Clasificación por nacionalidad", title_style))
    story.append(Spacer(1, 14))

    rows = [
        [
            Paragraph("NACIONALIDAD", header_cell_style),
            Paragraph("CLIENTES", header_cell_style),
            Paragraph("PARTICIPACIÓN", header_cell_style),
        ]
    ]
    for row in report_rows:
        rows.append(
            [
                Paragraph(xml_escape(str(row["pais"]).title()), cell_style),
                Paragraph(str(row["cantidad_clientes"]), cell_style),
                Paragraph(f"{row['participacion']}%", cell_style),
            ]
        )

    if report_rows:
        rows.append(
            [
                Paragraph("Total general", total_label_style),
                Paragraph(str(total_clientes), total_label_style),
                Paragraph("100%", total_label_style),
            ]
        )
    else:
        rows.append(
            [
                Paragraph("No hay clientes para incluir en el reporte.", empty_style),
                "",
                "",
            ]
        )

    table = Table(
        rows, colWidths=[doc.width * 0.5, doc.width * 0.22, doc.width * 0.28]
    )
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), pink_soft),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, line),
    ]
    if report_rows:
        table_style.extend(
            [
                ("LINEABOVE", (0, -1), (-1, -1), 0.8, line),
                ("LINEBELOW", (0, -1), (-1, -1), 0, line),
            ]
        )
    else:
        table_style.append(("SPAN", (0, -1), (-1, -1)))
    table.setStyle(TableStyle(table_style))
    story.append(table)

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.6)
        canvas.line(
            document.leftMargin,
            15 * mm,
            letter[0] - document.rightMargin,
            15 * mm,
        )
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(
            document.leftMargin, 10.5 * mm, "CandyKane · Reporte CRM de clientes"
        )
        canvas.drawRightString(
            letter[0] - document.rightMargin, 10.5 * mm, f"Página {document.page}"
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    buffer.seek(0)
    return buffer


def build_invoice_pdf(factura, items):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title=f"Factura {factura['numero_factura']}",
        author="CandyKane",
        subject="Factura de venta",
    )

    ink = colors.HexColor("#2A121D")
    muted = colors.HexColor("#7D6671")
    pink = colors.HexColor("#A83E69")
    pink_soft = colors.HexColor("#F9E9EF")
    line = colors.HexColor("#EDDEE4")

    brand_style = ParagraphStyle(
        "Brand", fontName="Times-Bold", fontSize=22, leading=24, textColor=pink
    )
    tagline_style = ParagraphStyle(
        "Tagline", fontName="Helvetica", fontSize=8, leading=11, textColor=muted
    )
    meta_label_style = ParagraphStyle(
        "MetaLabel",
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=10,
        textColor=pink,
        alignment=TA_RIGHT,
    )
    meta_number_style = ParagraphStyle(
        "MetaNumber",
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=17,
        textColor=ink,
        alignment=TA_RIGHT,
    )
    meta_date_style = ParagraphStyle(
        "MetaDate",
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=muted,
        alignment=TA_RIGHT,
    )
    label_style = ParagraphStyle(
        "Label", fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=pink
    )
    label_right_style = ParagraphStyle(
        "LabelRight", parent=label_style, alignment=TA_RIGHT
    )
    party_style = ParagraphStyle(
        "Party", fontName="Helvetica-Bold", fontSize=9.5, leading=13, textColor=ink
    )
    party_right_style = ParagraphStyle(
        "PartyRight", parent=party_style, alignment=TA_RIGHT
    )
    info_style = ParagraphStyle(
        "Info", fontName="Helvetica", fontSize=8.5, leading=12, textColor=muted
    )
    header_cell_style = ParagraphStyle(
        "HeaderCell",
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=10,
        textColor=pink,
    )
    header_right_style = ParagraphStyle(
        "HeaderRight", parent=header_cell_style, alignment=TA_RIGHT
    )
    cell_style = ParagraphStyle(
        "Cell", fontName="Helvetica", fontSize=8.5, leading=11, textColor=ink
    )
    cell_right_style = ParagraphStyle(
        "CellRight", parent=cell_style, alignment=TA_RIGHT
    )
    total_label_style = ParagraphStyle(
        "TotalLabel", fontName="Helvetica", fontSize=9, leading=12, textColor=muted
    )
    total_value_style = ParagraphStyle(
        "TotalValue",
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=ink,
        alignment=TA_RIGHT,
    )
    total_final_label_style = ParagraphStyle(
        "TotalFinalLabel",
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=14,
        textColor=ink,
    )
    total_final_value_style = ParagraphStyle(
        "TotalFinalValue", parent=total_final_label_style, alignment=TA_RIGHT
    )
    thanks_style = ParagraphStyle(
        "Thanks",
        fontName="Helvetica",
        fontSize=8.5,
        leading=12,
        textColor=muted,
        alignment=TA_CENTER,
    )

    story = []

    brand_cell = [
        Paragraph("CandyKane", brand_style),
        Spacer(1, 3),
        Paragraph("Postrería · CRM + POS", tagline_style),
    ]
    meta_cell = [
        Paragraph("FACTURA DE VENTA", meta_label_style),
        Spacer(1, 4),
        Paragraph(xml_escape(factura["numero_factura"]), meta_number_style),
        Spacer(1, 3),
        Paragraph(format_date_es(factura["fecha_emision"]), meta_date_style),
    ]
    header = Table(
        [[brand_cell, meta_cell]], colWidths=[doc.width * 0.56, doc.width * 0.44]
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 10))
    story.append(
        HRFlowable(width="100%", thickness=0.8, color=line, spaceBefore=0, spaceAfter=0)
    )
    story.append(Spacer(1, 14))

    contact_parts = []
    if factura.get("telefono_cliente"):
        contact_parts.append(f"Teléfono: {factura['telefono_cliente']}")
    if factura.get("email_cliente"):
        contact_parts.append(factura["email_cliente"])
    client_cell = [
        Paragraph("FACTURADO A", label_style),
        Spacer(1, 4),
        Paragraph(xml_escape(factura["cliente"]), party_style),
    ]
    if contact_parts:
        client_cell.append(Spacer(1, 2))
        client_cell.append(Paragraph(xml_escape(" · ".join(contact_parts)), info_style))
    seller_cell = [
        Paragraph("ATENDIDO POR", label_right_style),
        Spacer(1, 4),
        Paragraph(xml_escape(factura["vendedor"]), party_right_style),
    ]
    parties = Table(
        [[client_cell, seller_cell]], colWidths=[doc.width * 0.58, doc.width * 0.42]
    )
    parties.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(parties)
    story.append(Spacer(1, 18))

    item_rows = [
        [
            Paragraph("PRODUCTO", header_cell_style),
            Paragraph("CANT.", header_right_style),
            Paragraph("PRECIO", header_right_style),
            Paragraph("SUBTOTAL", header_right_style),
        ]
    ]
    subtotal = Decimal("0.00")
    for item in items:
        quantity = int(item["cantidad"])
        line_subtotal = Decimal(str(item["subtotal"]))
        subtotal += line_subtotal
        unit_price = (
            (line_subtotal / quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if quantity
            else Decimal("0.00")
        )
        item_rows.append(
            [
                Paragraph(xml_escape(item["nombre_producto"]), cell_style),
                Paragraph(str(quantity), cell_right_style),
                Paragraph(format_currency(unit_price), cell_right_style),
                Paragraph(format_currency(line_subtotal), cell_right_style),
            ]
        )

    if len(item_rows) == 1:
        item_rows.append(
            [
                Paragraph("Sin detalle", cell_style),
                Paragraph("—", cell_right_style),
                Paragraph("—", cell_right_style),
                Paragraph("—", cell_right_style),
            ]
        )

    items_table = Table(
        item_rows,
        colWidths=[doc.width * 0.5, doc.width * 0.1, doc.width * 0.2, doc.width * 0.2],
        repeatRows=1,
    )
    items_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), pink_soft),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("LINEBELOW", (0, 1), (-1, -1), 0.5, line),
            ]
        )
    )
    story.append(items_table)
    story.append(Spacer(1, 12))

    totals_rows = [
        [
            Paragraph("Subtotal", total_label_style),
            Paragraph(format_currency(subtotal), total_value_style),
        ],
        [
            Paragraph("Total", total_final_label_style),
            Paragraph(format_currency(factura["total"]), total_final_value_style),
        ],
    ]
    totals = Table(
        totals_rows, colWidths=[doc.width * 0.2, doc.width * 0.22], hAlign="RIGHT"
    )
    totals.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("LINEABOVE", (0, 1), (-1, 1), 0.8, line),
                ("TOPPADDING", (0, 1), (-1, 1), 8),
            ]
        )
    )
    story.append(
        KeepTogether(
            [
                totals,
                Spacer(1, 26),
                Paragraph("Gracias por endulzar tu día con nosotros.", thanks_style),
            ]
        )
    )

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(line)
        canvas.setLineWidth(0.6)
        canvas.line(
            document.leftMargin,
            15 * mm,
            letter[0] - document.rightMargin,
            15 * mm,
        )
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(muted)
        canvas.drawString(
            document.leftMargin, 10.5 * mm, "CandyKane · Factura de venta"
        )
        canvas.drawRightString(
            letter[0] - document.rightMargin, 10.5 * mm, f"Página {document.page}"
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    buffer.seek(0)
    return buffer
