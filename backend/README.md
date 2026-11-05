# Backend Flask - Kane

## Instalación

Desde PowerShell, en esta carpeta:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edita `.env` con tus credenciales de MySQL y una clave secreta larga para
`SECRET_KEY`. No compartas ni subas ese archivo.

## Crear el primer administrador

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

El módulo 3 todavía es un marcador de posición.
