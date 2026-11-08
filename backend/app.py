"""Arranque y configuración de CandyKane."""

import os
from secrets import token_hex

import click
from dotenv import load_dotenv
from flask import Flask
from flask_wtf.csrf import CSRFProtect
from mysql.connector import Error

import services
from routes import register_routes

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

app.jinja_env.filters["currency"] = services.format_currency

register_routes(app)


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

    try:
        services.create_admin_record(username, email, password)
        click.echo("Administrador creado correctamente.")
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    except Error as error:
        raise click.ClickException(
            f"No se pudo crear el administrador: {error}"
        ) from error


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
