# Dashboard Sources

`index.source.html` is the editable markup. `source/` contains the existing
styles and JavaScript, grouped by responsibility and included in source order.

Build the delivered page and Flask fallback together:

```sh
python scripts/generate_dashboard_fallback.py
```

Check both generated artifacts without changing files:

```sh
python scripts/build_dashboard.py --check
python scripts/check_dashboard_fallback.py
```

AstrBot still loads `index.html`, and Flask still serves the same self-contained
page. JavaScript fragments are assembled into the original classic script
blocks, not executed as separate modules. This preserves global declarations,
function hoisting, inline event handlers and initialization order without
requiring additional asset routes or a browser build dependency.

Do not edit generated `index.html` or `webui/index_fallback.py` directly.
