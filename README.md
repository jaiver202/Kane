# Backend Flask - Kane

## Instalación

Desde PowerShell, en la raíz del proyecto:

```powershell
Set-Location .\backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Las plantillas HTML de la interfaz están en `frontend\templates`, organizadas
por módulo (`clientes`, `productos`, `usuarios` y `ventas`).

Edita `.env` con tus credenciales de MySQL y una clave secreta larga para
`SECRET_KEY`. No compartas ni subas ese archivo.

## Crear el primer administrador

Si la tabla `usuarios` todavía no existe, selecciona la base de datos `kane_db`
en MySQL Workbench y ejecuta el contenido de `schema_usuarios.sql`.

Con el entorno virtual activado, ejecuta:

```powershell
flask --app app crear-admin
```

La contraseña se solicita de forma oculta y se almacena como hash.

## Ejecutar y probar

```powershell
python app.py
```

- `http://127.0.0.1:5000/` muestra el inicio de sesión.
- `http://127.0.0.1:5000/db-check` comprueba la conexión y lista las tablas.

El administrador ve los módulos 1, 2 y 3; el vendedor ve los módulos 1 y 2.
El módulo 1 permite crear, consultar, editar y eliminar clientes y generar un
reporte imprimible agrupado por país. No se puede eliminar un cliente que tenga
ventas relacionadas. Las cuentas deben tener contraseñas guardadas como hash
de Werkzeug y estado `activo`.

El módulo 2 permite registrar productos, ajustar existencias, crear ventas con
uno o varios productos, descontar stock y guardar la factura en
`historial_facturas`. Las operaciones de una venta se confirman en una sola
transacción. Las tablas `producto`, `stock`, `venta`, `detalle_venta` y
`historial_facturas` deben tener las columnas indicadas en el esquema de Kane;
cada producto debe tener exactamente un registro en `stock`.

El módulo 3 permite a los administradores crear, consultar, editar y eliminar
usuarios con rol de administrador o vendedor. Las contraseñas se guardan como
hash; al editar un usuario, deja el campo de nueva contraseña vacío para
conservar la contraseña actual. No se puede eliminar la propia cuenta ni
desactivar o eliminar al último administrador activo.
