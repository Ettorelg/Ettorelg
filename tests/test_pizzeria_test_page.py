"""The pizzeria rehearsal is private and never exposes an order endpoint."""
import ast
import copy
from pathlib import Path

from flask import Flask, abort, redirect, render_template, session, url_for


ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
NODE = copy.deepcopy(next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                          and node.name == "pizzeria_test_page"))
NODE.decorator_list = []
APP = Flask(__name__, template_folder=str(ROOT / "templates"))
APP.secret_key = "test-only"
APP.add_url_rule("/login", "login", lambda: "Login")
APP.add_url_rule("/dashboard_user", "dashboard_user", lambda: "Dashboard")


def page(shop_id=7):
    scope = {"session": session, "redirect": redirect, "url_for": url_for, "abort": abort,
             "render_template": render_template, "get_user_shop_id": lambda _: shop_id}
    exec(compile(ast.Module(body=[NODE], type_ignores=[]), "app.py", "exec"), scope)
    return scope["pizzeria_test_page"]


def test_anonymous_is_redirected_to_login():
    with APP.test_request_context("/pizzeria/prova"):
        assert page()().location == "/login"


def test_employee_and_admin_are_forbidden():
    for key in ("employee_id", "is_admin"):
        with APP.test_request_context("/pizzeria/prova"):
            session["user_id"] = 3
            session[key] = True
            try:
                page()()
            except Exception as exc:
                assert getattr(exc, "code", None) == 403
            else:
                raise AssertionError("Access to the private rehearsal was not blocked")


def test_owner_sees_dry_run_only():
    with APP.test_request_context("/pizzeria/prova"):
        session["user_id"] = 3
        APP.jinja_env.globals["csrf_token"] = lambda: "test-token"
        html = page()()
    assert "Prova il percorso del cliente" in html
    assert "non attiva il menu pubblico" in html
    assert "'/api/pizzeria'" in html and "base+'/preventivo'" in html
    assert "/api/ordini" not in html


def test_owner_without_shop_goes_to_setup():
    with APP.test_request_context("/pizzeria/prova"):
        session["user_id"] = 3
        assert page(None)().location == "/dashboard_user#attivita"
