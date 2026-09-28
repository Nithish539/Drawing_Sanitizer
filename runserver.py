# -*- coding: utf-8 -*-
"""Development server for Drawing_Analyzer.

Debug mode is off unless FLASK_DEBUG is set. The Werkzeug interactive
debugger allows remote code execution, so it must never be on by default
on a host that anything else can reach.
"""

from os import environ, path

from Drawing_Analyzer import app

_HERE = path.join(path.dirname(path.abspath(__file__)), "Drawing_Analyzer")
# Rules and rates are read once at import. They are meant to be edited by
# hand, so the reloader watches them like source.
_CONFIG_FILES = [path.join(_HERE, "family_rules.json"),
                 path.join(_HERE, "config.json")]


def _flag(name, default=False):
    raw = environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


if __name__ == "__main__":
    host = environ.get("SERVER_HOST", "127.0.0.1")
    try:
        port = int(environ.get("SERVER_PORT", "5555"))
    except ValueError:
        port = 5555
    debug = _flag("FLASK_DEBUG", False)
    if debug and host not in ("127.0.0.1", "localhost"):
        raise SystemExit(
            "Refusing to run the debugger on a non-loopback host. "
            "Unset FLASK_DEBUG or bind to 127.0.0.1.")
    # Restart when a .py file changes. Templates and static files are
    # already picked up live, so without this the browser runs new
    # JavaScript against old routes and every call fails as an HTML 404
    # parsed as JSON. This is the reloader only - the Werkzeug debugger,
    # which allows remote code execution, stays off unless FLASK_DEBUG
    # asks for it. Set SERVER_RELOAD=0 to disable.
    reload_on_edit = _flag("SERVER_RELOAD", True)
    app.run(host=host, port=port, debug=debug, use_reloader=reload_on_edit,
            extra_files=[f for f in _CONFIG_FILES if path.exists(f)])
