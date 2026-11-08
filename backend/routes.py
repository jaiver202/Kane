"""Rutas HTTP de CandyKane."""

from datetime import datetime
from functools import wraps

from flask import (
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
from mysql.connector import Error, IntegrityError

import services

MAX_STOCK = 2147483647


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


def _date_label(value):
    return services.format_date_es(value).replace(".", "")


def register_routes(app):
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

            try:
                user = services.authenticate_user(email, password)
            except Error:
                app.logger.exception(
                    "No se pudo consultar el usuario para iniciar sesión."
                )
                return render_template(
                    "login.html",
                    error="No se pudo conectar con la base de datos. Intenta de nuevo.",
                    email=email,
                ), 503

            if user is not None:
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
        try:
            dashboard_data = services.get_dashboard_data()
        except Error:
            app.logger.exception("No se pudo cargar el panel principal.")
            abort(503, description="No se pudo cargar el panel principal.")

        fecha = datetime.now()
        dias = (
            "lunes",
            "martes",
            "miércoles",
            "jueves",
            "viernes",
            "sábado",
            "domingo",
        )
        meses = (
            "enero",
            "febrero",
            "marzo",
            "abril",
            "mayo",
            "junio",
            "julio",
            "agosto",
            "septiembre",
            "octubre",
            "noviembre",
            "diciembre",
        )
        fecha_actual = (
            f"{dias[fecha.weekday()]}, {fecha.day} de {meses[fecha.month - 1]}"
        )

        return render_template(
            "dashboard.html",
            nombre_usuario=session["nombre_usuario"],
            rol=session["rol"],
            fecha_actual=fecha_actual,
            **dashboard_data,
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

    # ------------------------------------------------------------------
    # Usuarios
    # ------------------------------------------------------------------

    @app.get("/usuarios")
    @roles_required("administrador")
    def users():
        try:
            user_list = services.get_users()
        except Error:
            app.logger.exception("No se pudo consultar la lista de usuarios.")
            abort(503, description="No se pudo cargar la lista de usuarios.")

        return render_template("usuarios/usuarios.html", usuarios=user_list)

    @app.post("/usuarios/crear")
    @roles_required("administrador")
    def create_user():
        try:
            username, email, password, role, state = parse_user_form(
                require_password=True
            )
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("users"))

        try:
            services.create_user_record(username, email, password, role, state)
            flash("Usuario creado correctamente.", "success")
        except IntegrityError:
            flash("Ya existe un usuario registrado con ese correo.", "error")
        except Error:
            app.logger.exception("No se pudo crear el usuario.")
            flash("No se pudo guardar el usuario. Inténtalo de nuevo.", "error")

        return redirect(url_for("users"))

    @app.route("/usuarios/<int:user_id>/editar", methods=["GET", "POST"])
    @roles_required("administrador")
    def edit_user(user_id):
        if request.method == "POST":
            try:
                username, email, password, role, state = parse_user_form()
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("edit_user", user_id=user_id))

            if user_id == session["id_usuario"] and (
                role != "administrador" or state != "activo"
            ):
                flash(
                    "No puedes cambiar tu propio rol ni desactivar tu cuenta.", "error"
                )
                return redirect(url_for("edit_user", user_id=user_id))

            try:
                services.update_user_record(
                    user_id, username, email, password, role, state
                )
                flash("Usuario actualizado correctamente.", "success")
                return redirect(url_for("users"))
            except services.NotFoundError:
                abort(404)
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("edit_user", user_id=user_id))
            except IntegrityError:
                flash("Ya existe un usuario registrado con ese correo.", "error")
                return redirect(url_for("edit_user", user_id=user_id))
            except Error:
                app.logger.exception("No se pudo actualizar el usuario %s.", user_id)
                flash("No se pudo actualizar el usuario. Inténtalo de nuevo.", "error")
                return redirect(url_for("edit_user", user_id=user_id))

        try:
            user = services.get_user(user_id)
        except Error:
            app.logger.exception("No se pudo cargar el usuario %s.", user_id)
            abort(503, description="No se pudo cargar el usuario.")

        if user is None:
            abort(404)
        return render_template("usuarios/usuario_editar.html", usuario=user)

    @app.post("/usuarios/<int:user_id>/eliminar")
    @roles_required("administrador")
    def delete_user(user_id):
        if user_id == session["id_usuario"]:
            flash("No puedes eliminar tu propia cuenta.", "error")
            return redirect(url_for("users"))

        try:
            services.delete_user_record(user_id)
            flash("Usuario eliminado correctamente.", "success")
        except services.NotFoundError:
            abort(404)
        except ValueError as error:
            flash(str(error), "error")
        except IntegrityError:
            flash(
                "No se puede eliminar este usuario porque tiene registros relacionados.",
                "error",
            )
        except Error:
            app.logger.exception("No se pudo eliminar el usuario %s.", user_id)
            flash("No se pudo eliminar el usuario. Inténtalo de nuevo.", "error")

        return redirect(url_for("users"))

    # ------------------------------------------------------------------
    # Productos y stock
    # ------------------------------------------------------------------

    @app.get("/productos")
    @roles_required("administrador", "vendedor")
    def products():
        try:
            product_list = services.get_products()
        except Error:
            app.logger.exception("No se pudo consultar la lista de productos.")
            abort(503, description="No se pudo cargar la lista de productos.")

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
            price = services.parse_money(request.form.get("precio", ""), "El precio")
            quantity = int(raw_stock)
            if not name or len(name) > 100:
                raise ValueError(
                    "El nombre es obligatorio y no debe exceder 100 caracteres."
                )
            if quantity < 0 or quantity > MAX_STOCK:
                raise ValueError(
                    "La existencia inicial debe ser un entero no negativo."
                )
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for(target_endpoint))

        try:
            services.create_product_record(name, price, description, quantity)
            flash("Producto creado con su existencia inicial.", "success")
        except Error:
            app.logger.exception("No se pudo crear el producto y su existencia.")
            flash(
                "No se pudo guardar el producto. Revisa los datos e inténtalo de nuevo.",
                "error",
            )

        return redirect(url_for(target_endpoint))

    @app.route("/productos/<int:product_id>/editar", methods=["GET", "POST"])
    @roles_required("administrador", "vendedor")
    def edit_product(product_id):
        if request.method == "POST":
            name = request.form.get("nombre_producto", "").strip()
            description = request.form.get("descripcion", "").strip() or None
            raw_stock = request.form.get("cantidad_disponible", "").strip()
            from_products_page = request.form.get("origen") == "productos"

            try:
                price = services.parse_money(
                    request.form.get("precio", ""), "El precio"
                )
                if not name or len(name) > 100:
                    raise ValueError(
                        "El nombre es obligatorio y no debe exceder 100 caracteres."
                    )
                quantity = None
                if raw_stock:
                    quantity = int(raw_stock)
                    if quantity < 0 or quantity > MAX_STOCK:
                        raise ValueError(
                            "La existencia debe ser un entero no negativo."
                        )
            except ValueError as error:
                flash(str(error), "error")
                if from_products_page:
                    return redirect(url_for("products"))
                return redirect(url_for("edit_product", product_id=product_id))

            try:
                stock_warning = services.update_product_record(
                    product_id, name, price, description, quantity
                )
                if stock_warning:
                    flash(
                        "Producto actualizado. No se modificaron las existencias porque tiene varios registros de stock.",
                        "warning",
                    )
                else:
                    flash("Producto actualizado correctamente.", "success")
                return redirect(url_for("products"))
            except services.NotFoundError:
                abort(404)
            except Error:
                app.logger.exception(
                    "No se pudo actualizar el producto %s.", product_id
                )
                flash(
                    "No se pudo actualizar el producto. Inténtalo de nuevo.", "error"
                )
                if from_products_page:
                    return redirect(url_for("products"))
                return redirect(url_for("edit_product", product_id=product_id))

        try:
            product = services.get_product(product_id)
        except Error:
            app.logger.exception("No se pudo cargar el producto %s.", product_id)
            abort(503, description="No se pudo cargar el producto.")

        if product is None:
            abort(404)
        return render_template("productos/producto_editar.html", producto=product)

    @app.post("/productos/<int:product_id>/stock")
    @roles_required("administrador", "vendedor")
    def add_stock(product_id):
        try:
            quantity_to_add = int(request.form.get("cantidad", ""))
            if quantity_to_add <= 0 or quantity_to_add > MAX_STOCK:
                raise ValueError
        except ValueError:
            flash("La cantidad a agregar debe ser un entero positivo.", "error")
            return redirect(url_for("products"))

        try:
            services.add_stock_record(product_id, quantity_to_add)
            flash("Existencias actualizadas correctamente.", "success")
        except ValueError as error:
            flash(str(error), "error")
        except Error:
            app.logger.exception(
                "No se pudo aumentar el stock del producto %s.", product_id
            )
            flash("No se pudo actualizar el stock. Inténtalo de nuevo.", "error")

        return redirect(url_for("products"))

    # ------------------------------------------------------------------
    # Ventas
    # ------------------------------------------------------------------

    @app.get("/ventas")
    @roles_required("administrador", "vendedor")
    def sales():
        try:
            client_list, product_list = services.get_sales_data()
        except Error:
            app.logger.exception("No se pudo cargar el módulo de ventas.")
            abort(503, description="No se pudo cargar el módulo de ventas.")

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
            price = services.parse_money(request.form.get("precio", ""), "El precio")
            if not name or len(name) > 100:
                raise ValueError(
                    "El nombre es obligatorio y no debe exceder 100 caracteres."
                )
            quantity = None
            if raw_stock:
                quantity = int(raw_stock)
                if quantity < 0 or quantity > MAX_STOCK:
                    raise ValueError("La existencia debe ser un entero no negativo.")
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("sales"))

        try:
            stock_warning = services.update_product_record(
                product_id, name, price, description, quantity
            )
            if stock_warning:
                flash(
                    "Producto actualizado. No se modificaron las existencias porque tiene varios registros de stock.",
                    "warning",
                )
            else:
                flash("Producto actualizado correctamente.", "success")
        except services.NotFoundError:
            abort(404)
        except Error:
            app.logger.exception(
                "No se pudo actualizar el producto %s desde ventas.", product_id
            )
            flash("No se pudo actualizar el producto. Inténtalo de nuevo.", "error")

        return redirect(url_for("sales"))

    @app.post("/ventas/crear")
    @roles_required("administrador", "vendedor")
    def create_sale():
        try:
            client_id = int(request.form.get("id_cliente", ""))
            raw_product_ids = request.form.getlist("id_producto")
            raw_quantities = request.form.getlist("cantidad")
            if (
                client_id <= 0
                or not raw_product_ids
                or len(raw_product_ids) != len(raw_quantities)
            ):
                raise ValueError("Selecciona un cliente y al menos un producto.")

            requested_items = {}
            for raw_product_id, raw_quantity in zip(
                raw_product_ids, raw_quantities
            ):
                product_id = int(raw_product_id)
                quantity = int(raw_quantity)
                if product_id <= 0 or quantity <= 0:
                    raise ValueError(
                        "Cada producto debe tener una cantidad positiva."
                    )
                requested_items[product_id] = (
                    requested_items.get(product_id, 0) + quantity
                )
                if requested_items[product_id] > MAX_STOCK:
                    raise ValueError(
                        "La cantidad solicitada excede el máximo permitido."
                    )
        except ValueError as error:
            flash(str(error) or "Los datos de la venta no son válidos.", "error")
            return redirect(url_for("sales"))

        try:
            invoice_id = services.create_sale_record(
                client_id, session["id_usuario"], requested_items
            )
            flash("Venta registrada y factura generada correctamente.", "success")
            return redirect(url_for("invoice", invoice_id=invoice_id))
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("sales"))
        except Error:
            app.logger.exception("No se pudo registrar la venta.")
            flash("No se pudo completar la venta. No se guardaron cambios.", "error")
            return redirect(url_for("sales"))

    # ------------------------------------------------------------------
    # Facturas
    # ------------------------------------------------------------------

    @app.get("/facturas")
    @roles_required("administrador", "vendedor")
    def invoices():
        try:
            facturas, invoices_json = services.get_invoices()
        except Error:
            app.logger.exception("No se pudo cargar el historial de facturas.")
            abort(503, description="No se pudo cargar el historial de facturas.")

        return render_template(
            "ventas/facturas.html",
            facturas=facturas,
            facturas_json=invoices_json,
        )

    @app.get("/facturas/<int:invoice_id>")
    @roles_required("administrador", "vendedor")
    def invoice(invoice_id):
        try:
            invoice_data, items = services.get_invoice_by_id(invoice_id)
        except Error:
            app.logger.exception("No se pudo cargar la factura %s.", invoice_id)
            abort(503, description="No se pudo cargar la factura.")

        if invoice_data is None:
            abort(404)

        return render_template(
            "ventas/factura.html",
            factura=invoice_data,
            items=items,
            fecha_factura=_date_label(invoice_data["fecha_emision"]),
        )

    @app.get("/facturas/<int:invoice_id>/pdf")
    @roles_required("administrador", "vendedor")
    def invoice_pdf(invoice_id):
        try:
            factura, items = services.get_invoice_by_id(invoice_id)
        except Error:
            app.logger.exception("No se pudo generar la factura %s.", invoice_id)
            abort(503, description="No se pudo generar la factura.")

        if factura is None:
            abort(404)

        pdf_buffer = services.build_invoice_pdf(factura, items)
        return send_file(
            pdf_buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"factura-{factura['numero_factura']}.pdf",
        )

    # ------------------------------------------------------------------
    # Clientes
    # ------------------------------------------------------------------

    @app.get("/clientes")
    @roles_required("administrador", "vendedor")
    def clients():
        try:
            client_list, report_rows, total_clientes_reporte = (
                services.get_clients_page_data()
            )
        except Error:
            app.logger.exception("No se pudo consultar la lista de clientes.")
            abort(503, description="No se pudo cargar la lista de clientes.")

        return render_template(
            "clientes/clientes.html",
            clientes=client_list,
            reporte=report_rows,
            total_clientes_reporte=total_clientes_reporte,
            fecha_reporte=_date_label(datetime.now()),
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

        try:
            services.create_client_record(
                nombre, pais, email, telefono, direccion, ciudad
            )
            flash("Cliente creado correctamente.", "success")
        except Error:
            app.logger.exception("No se pudo crear el cliente.")
            flash(
                "No se pudo guardar el cliente. Revisa los datos e inténtalo de nuevo.",
                "error",
            )

        return redirect(url_for("clients"))

    @app.route("/clientes/<int:client_id>/editar", methods=["GET", "POST"])
    @roles_required("administrador", "vendedor")
    def edit_client(client_id):
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
                services.update_client_record(
                    client_id, nombre, pais, email, telefono, direccion, ciudad
                )
                flash("Cliente actualizado correctamente.", "success")
                return redirect(url_for("clients"))
            except services.NotFoundError:
                abort(404)
            except Error:
                app.logger.exception(
                    "No se pudo actualizar el cliente %s.", client_id
                )
                flash(
                    "No se pudo actualizar el cliente. Inténtalo de nuevo.", "error"
                )
                if from_clients_page:
                    return redirect(url_for("clients"))
                return redirect(url_for("edit_client", client_id=client_id))

        try:
            client = services.get_client(client_id)
        except Error:
            app.logger.exception("No se pudo cargar el cliente %s.", client_id)
            abort(503, description="No se pudo cargar el cliente.")

        if client is None:
            abort(404)
        return render_template("clientes/cliente_editar.html", cliente=client)

    @app.post("/clientes/<int:client_id>/eliminar")
    @roles_required("administrador", "vendedor")
    def delete_client(client_id):
        try:
            services.delete_client_record(client_id)
            flash("Cliente eliminado correctamente.", "success")
        except services.NotFoundError:
            abort(404)
        except Error:
            app.logger.exception("No se pudo eliminar el cliente %s.", client_id)
            flash(
                "No se pudo eliminar el cliente. Puede tener ventas asociadas.",
                "error",
            )

        return redirect(url_for("clients"))

    @app.get("/clientes/reporte")
    @roles_required("administrador", "vendedor")
    def client_report():
        try:
            report_rows, total_clientes = services.get_client_report_data()
        except Error:
            app.logger.exception("No se pudo generar el reporte de clientes.")
            abort(503, description="No se pudo generar el reporte de clientes.")

        return render_template(
            "clientes/clientes_reporte.html",
            reporte=report_rows,
            total_clientes_reporte=total_clientes,
            fecha_reporte=_date_label(datetime.now()),
        )

    @app.get("/clientes/reporte/pdf")
    @roles_required("administrador", "vendedor")
    def client_report_pdf():
        try:
            report_rows, total_clientes = services.get_client_report_data()
        except Error:
            app.logger.exception("No se pudo generar el PDF del reporte de clientes.")
            abort(503, description="No se pudo generar el reporte de clientes.")

        pdf_buffer = services.build_client_report_pdf(
            report_rows,
            total_clientes,
            _date_label(datetime.now()),
        )
        return send_file(
            pdf_buffer,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"reporte-clientes-{datetime.now():%Y-%m-%d}.pdf",
        )

    # ------------------------------------------------------------------
    # Diagnóstico
    # ------------------------------------------------------------------

    @app.get("/db-check")
    def db_check():
        try:
            tables = services.get_table_names()
            return jsonify(
                status="connected",
                database=services.DB_CONFIG["database"],
                tables=tables,
            )
        except Error:
            app.logger.exception(
                "No se pudo conectar o consultar la base de datos."
            )
            return jsonify(
                status="error",
                message=(
                    "No se pudo acceder a MySQL. Verifica las credenciales, "
                    "que MySQL esté iniciado y que la base de datos exista."
                ),
            ), 503
