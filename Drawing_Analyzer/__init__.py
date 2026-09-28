# -*- coding: utf-8 -*-
"""The Drawing_Analyzer Flask package.

There used to be two competing applications here: this file plus views.py
defined one (a leftover Visual Studio template, with routes pointing at
templates that do not exist), and app.py defined the real one.
runserver.py imported the template app, so launching the project the
Visual Studio way produced a dead site with none of the analysis routes
registered.

views.py is gone and this package now exports the single real
application. The import is lazy so that the pure modules (geometry,
units, quantities) can be imported and tested without pulling in Flask
or ezdxf.
"""

__all__ = ["app", "create_app"]


def create_app():
    from .app import app as _app
    # Importing the .app submodule makes Python set this package's `app`
    # attribute to that submodule object, clobbering the lazy attribute
    # this function is trying to provide. Put the real Flask instance back
    # so `from Drawing_Analyzer import app` resolves to it, not the module.
    globals()["app"] = _app
    return _app


def __getattr__(name):
    if name == "app":
        return create_app()
    raise AttributeError("module {0!r} has no attribute {1!r}".format(
        __name__, name))
