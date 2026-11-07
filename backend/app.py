import io
import os
from functools import wraps
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from secrets import token_hex
from xml.sax.saxutils import escape as xml_escape

import click
import mysql.connector
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
from mysql.connector import Error, IntegrityError
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

app = Flask(
    __name__,
    template_folder=os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "frontend", "templates")
    ),
)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
CSRFProtect(app)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "kane_db"),
    "connection_timeout": 5,
}


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if "id_usuario" not in session:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped_view(**kwargs):
            if session.get("rol") not in roles:
                abort(403)
            return view(**kwargs)

        return wrapped_view

    return decorator


@app.get("/")
def home():
    if "id_usuario" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "id_usuario" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        connection = None

        try:
            connection = mysql.connector.connect(**DB_CONFIG)
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
        except Error:
            app.logger.exception("No se pudo consultar el usuario para iniciar sesión.")
            return render_template(
                "login.html",
                error="No se pudo conectar con la base de datos. Intenta de nuevo.",
                email=email,
            ), 503
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

        if (
            user
            and user[5] == "activo"
            and user[4] in ("administrador", "vendedor")
            and check_password_hash(user[3], password)
        ):
            session.clear()
            session["id_usuario"] = user[0]
            session["nombre_usuario"] = user[1]
            session["rol"] = user[4]
            return redirect(url_for("dashboard"))

        return render_template(
            "login.html",
            error="Correo o contraseña incorrectos, o usuario inactivo.",
            email=email,
        ), 401

    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
@login_required
def dashboard():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
    except Error:
        app.logger.exception("No se pudo cargar el panel principal.")
        abort(503, description="No se pudo cargar el panel principal.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    fecha = datetime.now()
    dias = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
    meses = (
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
    )
    fecha_actual = f"{dias[fecha.weekday()]}, {fecha.day} de {meses[fecha.month - 1]}"
    for venta in ventas_recientes:
        venta["fecha_display"] = format_date_es(venta["fecha_venta"])
        venta["total_display"] = format_currency(venta["total"])

    return render_template(
        "dashboard.html",
        nombre_usuario=session["nombre_usuario"],
        rol=session["rol"],
        fecha_actual=fecha_actual,
        total_clientes=total_clientes,
        ventas_hoy=metricas_ventas["ventas_hoy"],
        ventas_hoy_display=format_currency(metricas_ventas["ventas_hoy"]),
        transacciones_hoy=metricas_ventas["transacciones_hoy"],
        ventas_mes=metricas_ventas["ventas_mes"],
        ventas_mes_display=format_currency(metricas_ventas["ventas_mes"]),
        ventas_recientes=ventas_recientes,
        alertas_stock=alertas_stock,
        total_stock_bajo=total_stock_bajo,
    )


@app.get("/modulo/<int:module_id>")
@login_required
def module_placeholder(module_id):
    if module_id not in (1, 2, 3):
        abort(404)
    if module_id == 3 and session.get("rol") != "administrador":
        abort(403)
    if module_id == 1:
        return redirect(url_for("clients"))
    if module_id == 2:
        return redirect(url_for("sales"))
    if module_id == 3:
        return redirect(url_for("users"))

    titles = {
        1: "Gestión de clientes",
        2: "Registro de ventas",
        3: "Gestión de usuarios",
    }
    return render_template("module_placeholder.html", title=titles[module_id])


def parse_money(value, field_name):
    try:
        amount = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} debe ser un valor numérico válido.")
    if not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99"):
        raise ValueError(f"{field_name} debe estar entre 0 y 99,999,999.99.")
    return amount


def parse_user_form(require_password=False):
    username = request.form.get("nombre_usuario", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("contrasena", "")
    role = request.form.get("rol", "")
    state = request.form.get("estado", "")

    if not username or len(username) > 50:
        raise ValueError("El nombre es obligatorio y no debe exceder 50 caracteres.")
    if (
        not email
        or len(email) > 100
        or "@" not in email
        or "." not in email.rsplit("@", 1)[-1]
        or any(character.isspace() for character in email)
    ):
        raise ValueError("Ingresa un correo válido de máximo 100 caracteres.")
    if require_password and not password:
        raise ValueError("La contraseña es obligatoria.")
    if password and len(password) < 8:
        raise ValueError("La contraseña debe tener al menos 8 caracteres.")
    if role not in ("administrador", "vendedor"):
        raise ValueError("Selecciona un rol válido.")
    if state not in ("activo", "inactivo"):
        raise ValueError("Selecciona un estado válido.")

    return username, email, password, role, state


@app.get("/usuarios")
@roles_required("administrador")
def users():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_usuario, nombre_usuario, email, rol, estado
                FROM usuarios
                ORDER BY nombre_usuario, id_usuario
                """
            )
            user_list = cursor.fetchall()
    except Error:
        app.logger.exception("No se pudo consultar la lista de usuarios.")
        abort(503, description="No se pudo cargar la lista de usuarios.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return render_template("usuarios/usuarios.html", usuarios=user_list)


@app.post("/usuarios/crear")
@roles_required("administrador")
def create_user():
    try:
        username, email, password, role, state = parse_user_form(require_password=True)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("users"))

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO usuarios (nombre_usuario, email, contrasena, rol, estado)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (username, email, generate_password_hash(password), role, state),
            )
        connection.commit()
        flash("Usuario creado correctamente.", "success")
    except IntegrityError:
        if connection is not None:
            connection.rollback()
        flash("Ya existe un usuario registrado con ese correo.", "error")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo crear el usuario.")
        flash("No se pudo guardar el usuario. Inténtalo de nuevo.", "error")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for("users"))


@app.route("/usuarios/<int:user_id>/editar", methods=["GET", "POST"])
@roles_required("administrador")
def edit_user(user_id):
    connection = None
    if request.method == "POST":
        try:
            username, email, password, role, state = parse_user_form()
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("edit_user", user_id=user_id))

        if user_id == session["id_usuario"] and (
            role != "administrador" or state != "activo"
        ):
            flash("No puedes cambiar tu propio rol ni desactivar tu cuenta.", "error")
            return redirect(url_for("edit_user", user_id=user_id))

        try:
            connection = mysql.connector.connect(**DB_CONFIG)
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
                    connection.rollback()
                    abort(404)

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
                        connection.rollback()
                        flash("Debe permanecer al menos un administrador activo.", "error")
                        return redirect(url_for("edit_user", user_id=user_id))

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
            flash("Usuario actualizado correctamente.", "success")
            return redirect(url_for("users"))
        except IntegrityError:
            if connection is not None:
                connection.rollback()
            flash("Ya existe un usuario registrado con ese correo.", "error")
            return redirect(url_for("edit_user", user_id=user_id))
        except Error:
            if connection is not None:
                connection.rollback()
            app.logger.exception("No se pudo actualizar el usuario %s.", user_id)
            flash("No se pudo actualizar el usuario. Inténtalo de nuevo.", "error")
            return redirect(url_for("edit_user", user_id=user_id))
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_usuario, nombre_usuario, email, rol, estado
                FROM usuarios
                WHERE id_usuario = %s
                """,
                (user_id,),
            )
            user = cursor.fetchone()
    except Error:
        app.logger.exception("No se pudo cargar el usuario %s.", user_id)
        abort(503, description="No se pudo cargar el usuario.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    if user is None:
        abort(404)
    return render_template("usuarios/usuario_editar.html", usuario=user)


@app.post("/usuarios/<int:user_id>/eliminar")
@roles_required("administrador")
def delete_user(user_id):
    if user_id == session["id_usuario"]:
        flash("No puedes eliminar tu propia cuenta.", "error")
        return redirect(url_for("users"))

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
                connection.rollback()
                abort(404)

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
                    connection.rollback()
                    flash("Debe permanecer al menos un administrador activo.", "error")
                    return redirect(url_for("users"))

            cursor.execute("DELETE FROM usuarios WHERE id_usuario = %s", (user_id,))
        connection.commit()
        flash("Usuario eliminado correctamente.", "success")
    except IntegrityError:
        if connection is not None:
            connection.rollback()
        flash("No se puede eliminar este usuario porque tiene registros relacionados.", "error")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo eliminar el usuario %s.", user_id)
        flash("No se pudo eliminar el usuario. Inténtalo de nuevo.", "error")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for("users"))


@app.get("/productos")
@roles_required("administrador", "vendedor")
def products():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
            product_list = cursor.fetchall()
    except Error:
        app.logger.exception("No se pudo consultar la lista de productos.")
        abort(503, description="No se pudo cargar la lista de productos.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return render_template("productos/productos.html", productos=product_list)


@app.post("/productos/crear")
@roles_required("administrador", "vendedor")
def create_product():
    name = request.form.get("nombre_producto", "").strip()
    description = request.form.get("descripcion", "").strip() or None
    raw_stock = request.form.get("cantidad_disponible", "").strip()
    from_sales_page = request.form.get("origen") == "ventas"
    target_endpoint = "sales" if from_sales_page else "products"
    try:
        price = parse_money(request.form.get("precio", ""), "El precio")
        quantity = int(raw_stock)
        if not name or len(name) > 100:
            raise ValueError("El nombre es obligatorio y no debe exceder 100 caracteres.")
        if quantity < 0 or quantity > 2147483647:
            raise ValueError("La existencia inicial debe ser un entero no negativo.")
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for(target_endpoint))

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
        flash("Producto creado con su existencia inicial.", "success")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo crear el producto y su existencia.")
        flash("No se pudo guardar el producto. Revisa los datos e inténtalo de nuevo.", "error")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for(target_endpoint))


@app.route("/productos/<int:product_id>/editar", methods=["GET", "POST"])
@roles_required("administrador", "vendedor")
def edit_product(product_id):
    connection = None
    if request.method == "POST":
        name = request.form.get("nombre_producto", "").strip()
        description = request.form.get("descripcion", "").strip() or None
        raw_stock = request.form.get("cantidad_disponible", "").strip()
        from_products_page = request.form.get("origen") == "productos"
        try:
            price = parse_money(request.form.get("precio", ""), "El precio")
            if not name or len(name) > 100:
                raise ValueError("El nombre es obligatorio y no debe exceder 100 caracteres.")
            quantity = None
            if raw_stock:
                quantity = int(raw_stock)
                if quantity < 0 or quantity > 2147483647:
                    raise ValueError("La existencia debe ser un entero no negativo.")
        except ValueError as error:
            flash(str(error), "error")
            if from_products_page:
                return redirect(url_for("products"))
            return redirect(url_for("edit_product", product_id=product_id))

        connection = None
        stock_warning = False
        try:
            connection = mysql.connector.connect(**DB_CONFIG)
            connection.start_transaction()
            with connection.cursor(dictionary=True) as cursor:
                cursor.execute(
                    """
                    UPDATE producto
                    SET nombre_producto = %s, precio = %s, descripcion = %s
                    WHERE id_producto = %s
                    """,
                    (name, price, description, product_id),
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        "SELECT id_producto FROM producto WHERE id_producto = %s FOR UPDATE",
                        (product_id,),
                    )
                    if cursor.fetchone() is None:
                        connection.rollback()
                        abort(404)

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
            if stock_warning:
                flash(
                    "Producto actualizado. No se modificaron las existencias porque tiene varios registros de stock.",
                    "warning",
                )
            else:
                flash("Producto actualizado correctamente.", "success")
            return redirect(url_for("products"))
        except Error:
            if connection is not None:
                connection.rollback()
            app.logger.exception("No se pudo actualizar el producto %s.", product_id)
            flash("No se pudo actualizar el producto. Inténtalo de nuevo.", "error")
            if from_products_page:
                return redirect(url_for("products"))
            return redirect(url_for("edit_product", product_id=product_id))
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_producto, nombre_producto, precio, descripcion
                FROM producto WHERE id_producto = %s
                """,
                (product_id,),
            )
            product = cursor.fetchone()
    except Error:
        app.logger.exception("No se pudo cargar el producto %s.", product_id)
        abort(503, description="No se pudo cargar el producto.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    if product is None:
        abort(404)
    return render_template("productos/producto_editar.html", producto=product)


@app.post("/productos/<int:product_id>/stock")
@roles_required("administrador", "vendedor")
def add_stock(product_id):
    try:
        quantity_to_add = int(request.form.get("cantidad", ""))
        if quantity_to_add <= 0 or quantity_to_add > 2147483647:
            raise ValueError
    except ValueError:
        flash("La cantidad a agregar debe ser un entero positivo.", "error")
        return redirect(url_for("products"))

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        connection.start_transaction()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id_stock, cantidad_disponible FROM stock WHERE id_producto = %s FOR UPDATE",
                (product_id,),
            )
            stock_rows = cursor.fetchall()
            if len(stock_rows) != 1:
                connection.rollback()
                flash(
                    "No se pudo actualizar el stock: el producto no tiene un único registro de existencias.",
                    "error",
                )
                return redirect(url_for("products"))
            stock_id, available = stock_rows[0]
            if available + quantity_to_add > 2147483647:
                connection.rollback()
                flash("La existencia excedería el máximo permitido.", "error")
                return redirect(url_for("products"))
            cursor.execute(
                "UPDATE stock SET cantidad_disponible = %s WHERE id_stock = %s",
                (available + quantity_to_add, stock_id),
            )
        connection.commit()
        flash("Existencias actualizadas correctamente.", "success")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo aumentar el stock del producto %s.", product_id)
        flash("No se pudo actualizar el stock. Inténtalo de nuevo.", "error")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for("products"))


@app.get("/ventas")
@roles_required("administrador", "vendedor")
def sales():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
    except Error:
        app.logger.exception("No se pudo cargar el módulo de ventas.")
        abort(503, description="No se pudo cargar el módulo de ventas.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return render_template(
        "ventas/ventas.html",
        clientes=client_list,
        productos=product_list,
    )


@app.post("/ventas/productos/<int:product_id>/editar")
@roles_required("administrador", "vendedor")
def edit_sale_product(product_id):
    name = request.form.get("nombre_producto", "").strip()
    description = request.form.get("descripcion", "").strip() or None
    raw_stock = request.form.get("cantidad_disponible", "").strip()

    try:
        price = parse_money(request.form.get("precio", ""), "El precio")
        if not name or len(name) > 100:
            raise ValueError(
                "El nombre es obligatorio y no debe exceder 100 caracteres."
            )
        quantity = None
        if raw_stock:
            quantity = int(raw_stock)
            if quantity < 0 or quantity > 2147483647:
                raise ValueError("La existencia debe ser un entero no negativo.")
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("sales"))

    connection = None
    stock_warning = False
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        connection.start_transaction()
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT id_producto FROM producto WHERE id_producto = %s FOR UPDATE",
                (product_id,),
            )
            if cursor.fetchone() is None:
                connection.rollback()
                abort(404)

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
        if stock_warning:
            flash(
                "Producto actualizado. No se modificaron las existencias porque tiene varios registros de stock.",
                "warning",
            )
        else:
            flash("Producto actualizado correctamente.", "success")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception(
            "No se pudo actualizar el producto %s desde ventas.", product_id
        )
        flash("No se pudo actualizar el producto. Inténtalo de nuevo.", "error")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for("sales"))


@app.post("/ventas/crear")
@roles_required("administrador", "vendedor")
def create_sale():
    try:
        client_id = int(request.form.get("id_cliente", ""))
        raw_product_ids = request.form.getlist("id_producto")
        raw_quantities = request.form.getlist("cantidad")
        if client_id <= 0 or not raw_product_ids or len(raw_product_ids) != len(raw_quantities):
            raise ValueError("Selecciona un cliente y al menos un producto.")

        requested_items = {}
        for raw_product_id, raw_quantity in zip(raw_product_ids, raw_quantities):
            product_id = int(raw_product_id)
            quantity = int(raw_quantity)
            if product_id <= 0 or quantity <= 0:
                raise ValueError("Cada producto debe tener una cantidad positiva.")
            requested_items[product_id] = requested_items.get(product_id, 0) + quantity
            if requested_items[product_id] > 2147483647:
                raise ValueError("La cantidad solicitada excede el máximo permitido.")
    except ValueError as error:
        flash(str(error) or "Los datos de la venta no son válidos.", "error")
        return redirect(url_for("sales"))

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
                (client_id, session["id_usuario"], total),
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

            invoice_number = (
                f"FAC-{datetime.now():%Y%m%d}-{token_hex(4).upper()}"
            )
            cursor.execute(
                """
                INSERT INTO historial_facturas (id_venta, numero_factura)
                VALUES (%s, %s)
                """,
                (sale_id, invoice_number),
            )
            invoice_id = cursor.lastrowid

        connection.commit()
        flash("Venta registrada y factura generada correctamente.", "success")
        return redirect(url_for("invoice", invoice_id=invoice_id))
    except ValueError as error:
        if connection is not None:
            connection.rollback()
        flash(str(error), "error")
        return redirect(url_for("sales"))
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo registrar la venta.")
        flash("No se pudo completar la venta. No se guardaron cambios.", "error")
        return redirect(url_for("sales"))
    finally:
        if connection is not None and connection.is_connected():
            connection.close()


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


app.jinja_env.filters["currency"] = format_currency


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
        author="Dulce Gestión",
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
        Paragraph("Dulce Gestión", brand_style),
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
            document.leftMargin, 10.5 * mm, "Dulce Gestión · Reporte CRM de clientes"
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
        author="Dulce Gestión",
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
        Paragraph("Dulce Gestión", brand_style),
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
            document.leftMargin, 10.5 * mm, "Dulce Gestión · Factura de venta"
        )
        canvas.drawRightString(
            letter[0] - document.rightMargin, 10.5 * mm, f"Página {document.page}"
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    buffer.seek(0)
    return buffer


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


@app.get("/facturas")
@roles_required("administrador", "vendedor")
def invoices():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
    except Error:
        app.logger.exception("No se pudo cargar el historial de facturas.")
        abort(503, description="No se pudo cargar el historial de facturas.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    invoices_json = serialize_invoices(facturas, invoice_items)

    return render_template(
        "ventas/facturas.html",
        facturas=facturas,
        facturas_json=invoices_json,
    )


@app.get("/facturas/<int:invoice_id>")
@roles_required("administrador", "vendedor")
def invoice(invoice_id):
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
                abort(404)
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
    except Error:
        app.logger.exception("No se pudo cargar la factura %s.", invoice_id)
        abort(503, description="No se pudo cargar la factura.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    for item in items:
        item["precio_unitario"] = (
            Decimal(str(item["subtotal"])) / item["cantidad"]
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return render_template("ventas/factura.html", factura=invoice_data, items=items)


@app.get("/facturas/<int:invoice_id>/pdf")
@roles_required("administrador", "vendedor")
def invoice_pdf(invoice_id):
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
            factura = cursor.fetchone()
            if factura is None:
                abort(404)
            cursor.execute(
                """
                SELECT p.nombre_producto, d.cantidad, d.subtotal
                FROM detalle_venta AS d
                JOIN producto AS p ON p.id_producto = d.id_producto
                WHERE d.id_venta = %s
                ORDER BY d.id_detalle
                """,
                (factura["id_venta"],),
            )
            items = cursor.fetchall()
    except Error:
        app.logger.exception("No se pudo generar la factura %s.", invoice_id)
        abort(503, description="No se pudo generar la factura.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    pdf_buffer = build_invoice_pdf(factura, items)
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"factura-{factura['numero_factura']}.pdf",
    )


@app.get("/clientes")
@roles_required("administrador", "vendedor")
def clients():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_cliente, nombre, email, telefono, direccion, ciudad, pais,
                       fecha_registro
                FROM cliente
                ORDER BY id_cliente DESC
                """
            )
            client_list = cursor.fetchall()
    except Error:
        app.logger.exception("No se pudo consultar la lista de clientes.")
        abort(503, description="No se pudo cargar la lista de clientes.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    clientes_por_pais = {}
    for cliente in client_list:
        cliente["fecha_registro_texto"] = format_date_es(cliente["fecha_registro"]).replace(".", "")
        pais = cliente["pais"]
        clientes_por_pais[pais] = clientes_por_pais.get(pais, 0) + 1

    total_clientes_reporte = len(client_list)
    report_rows = [
        {
            "pais": pais,
            "cantidad_clientes": cantidad,
        }
        for pais, cantidad in sorted(
            clientes_por_pais.items(), key=lambda item: (-item[1], item[0].casefold())
        )
    ] if total_clientes_reporte else []
    add_report_participation(report_rows)

    return render_template(
        "clientes/clientes.html",
        clientes=client_list,
        reporte=report_rows,
        total_clientes_reporte=total_clientes_reporte,
        fecha_reporte=format_date_es(datetime.now()).replace(".", ""),
    )


@app.post("/clientes/crear")
@roles_required("administrador", "vendedor")
def create_client():
    nombre = request.form.get("nombre", "").strip()
    pais = request.form.get("pais", "").strip()
    email = request.form.get("email", "").strip() or None
    telefono = request.form.get("telefono", "").strip() or None
    direccion = request.form.get("direccion", "").strip() or None
    ciudad = request.form.get("ciudad", "").strip() or None

    if not nombre or not pais:
        flash("El nombre y el país son obligatorios.", "error")
        return redirect(url_for("clients"))
    if (
        len(nombre) > 100
        or len(pais) > 50
        or (email is not None and len(email) > 100)
        or (telefono is not None and len(telefono) > 20)
        or (direccion is not None and len(direccion) > 150)
        or (ciudad is not None and len(ciudad) > 50)
    ):
        flash("Uno o más campos exceden la longitud permitida.", "error")
        return redirect(url_for("clients"))

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO cliente (nombre, email, telefono, direccion, ciudad, pais)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (nombre, email, telefono, direccion, ciudad, pais),
            )
        connection.commit()
        flash("Cliente creado correctamente.", "success")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo crear el cliente.")
        flash("No se pudo guardar el cliente. Revisa los datos e inténtalo de nuevo.", "error")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for("clients"))


@app.route("/clientes/<int:client_id>/editar", methods=["GET", "POST"])
@roles_required("administrador", "vendedor")
def edit_client(client_id):
    connection = None
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        pais = request.form.get("pais", "").strip()
        email = request.form.get("email", "").strip() or None
        telefono = request.form.get("telefono", "").strip() or None
        direccion = request.form.get("direccion", "").strip() or None
        ciudad = request.form.get("ciudad", "").strip() or None
        from_clients_page = request.form.get("origen") == "clientes"

        if not nombre or not pais:
            flash("El nombre y el país son obligatorios.", "error")
            if from_clients_page:
                return redirect(url_for("clients"))
            return redirect(url_for("edit_client", client_id=client_id))
        if (
            len(nombre) > 100
            or len(pais) > 50
            or (email is not None and len(email) > 100)
            or (telefono is not None and len(telefono) > 20)
            or (direccion is not None and len(direccion) > 150)
            or (ciudad is not None and len(ciudad) > 50)
        ):
            flash("Uno o más campos exceden la longitud permitida.", "error")
            if from_clients_page:
                return redirect(url_for("clients"))
            return redirect(url_for("edit_client", client_id=client_id))

        try:
            connection = mysql.connector.connect(**DB_CONFIG)
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
                        connection.rollback()
                        abort(404)
            connection.commit()
            flash("Cliente actualizado correctamente.", "success")
            return redirect(url_for("clients"))
        except Error:
            if connection is not None:
                connection.rollback()
            app.logger.exception("No se pudo actualizar el cliente %s.", client_id)
            flash("No se pudo actualizar el cliente. Inténtalo de nuevo.", "error")
            if from_clients_page:
                return redirect(url_for("clients"))
            return redirect(url_for("edit_client", client_id=client_id))
        finally:
            if connection is not None and connection.is_connected():
                connection.close()

    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute(
                """
                SELECT id_cliente, nombre, email, telefono, direccion, ciudad, pais
                FROM cliente
                WHERE id_cliente = %s
                """,
                (client_id,),
            )
            client = cursor.fetchone()
    except Error:
        app.logger.exception("No se pudo cargar el cliente %s.", client_id)
        abort(503, description="No se pudo cargar el cliente.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    if client is None:
        abort(404)
    return render_template("clientes/cliente_editar.html", cliente=client)


@app.post("/clientes/<int:client_id>/eliminar")
@roles_required("administrador", "vendedor")
def delete_client(client_id):
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM cliente WHERE id_cliente = %s", (client_id,))
            if cursor.rowcount == 0:
                abort(404)
        connection.commit()
        flash("Cliente eliminado correctamente.", "success")
    except Error:
        if connection is not None:
            connection.rollback()
        app.logger.exception("No se pudo eliminar el cliente %s.", client_id)
        flash(
            "No se pudo eliminar el cliente. Puede tener ventas asociadas.",
            "error",
        )
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return redirect(url_for("clients"))


@app.get("/clientes/reporte")
@roles_required("administrador", "vendedor")
def client_report():
    connection = None
    report_rows = []
    total_clientes = 0
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
    except Error:
        app.logger.exception("No se pudo generar el reporte de clientes.")
        abort(503, description="No se pudo generar el reporte de clientes.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    return render_template(
        "clientes/clientes_reporte.html",
        reporte=report_rows,
        total_clientes_reporte=total_clientes,
        fecha_reporte=format_date_es(datetime.now()).replace(".", ""),
    )


@app.get("/clientes/reporte/pdf")
@roles_required("administrador", "vendedor")
def client_report_pdf():
    connection = None
    report_rows = []
    total_clientes = 0
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
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
    except Error:
        app.logger.exception("No se pudo generar el PDF del reporte de clientes.")
        abort(503, description="No se pudo generar el reporte de clientes.")
    finally:
        if connection is not None and connection.is_connected():
            connection.close()

    pdf_buffer = build_client_report_pdf(
        report_rows,
        total_clientes,
        format_date_es(datetime.now()).replace(".", ""),
    )
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"reporte-clientes-{datetime.now():%Y-%m-%d}.pdf",
    )



@app.get("/db-check")
def db_check():
    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            tables = [row[0] for row in cursor.fetchall()]

        return jsonify(
            status="connected",
            database=DB_CONFIG["database"],
            tables=tables,
        )
    except Error:
        app.logger.exception("No se pudo conectar o consultar la base de datos.")
        return jsonify(
            status="error",
            message=(
                "No se pudo acceder a MySQL. Verifica las credenciales, "
                "que MySQL esté iniciado y que la base de datos exista."
            ),
        ), 503
    finally:
        if connection is not None and connection.is_connected():
            connection.close()


@app.cli.command("crear-admin")
def create_admin():
    """Crea una cuenta inicial de administrador con contraseña hasheada."""
    username = click.prompt("Nombre de usuario")
    email = click.prompt("Correo")
    password = click.prompt(
        "Contraseña (mínimo 8 caracteres)",
        hide_input=True,
        confirmation_prompt=True,
    )
    if len(password) < 8:
        raise click.ClickException("La contraseña debe tener al menos 8 caracteres.")

    connection = None
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute("SELECT id_usuario FROM usuarios WHERE email = %s", (email,))
            if cursor.fetchone():
                raise click.ClickException("Ya existe un usuario con ese correo.")
            cursor.execute(
                """
                INSERT INTO usuarios (nombre_usuario, email, contrasena, rol, estado)
                VALUES (%s, %s, %s, 'administrador', 'activo')
                """,
                (username, email, generate_password_hash(password)),
            )
        connection.commit()
        click.echo("Administrador creado correctamente.")
    except Error as error:
        if connection is not None:
            connection.rollback()
        raise click.ClickException(f"No se pudo crear el administrador: {error}") from error
    finally:
        if connection is not None and connection.is_connected():
            connection.close()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
