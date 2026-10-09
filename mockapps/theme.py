"""Shared look for the mock company apps: a branded top bar and readable tables and forms.

Styling only. The HTML the operator reads (forms, labels, buttons, data-aiop-* attributes) is
unchanged, so nothing here can change what the agent sees or what the policy decides."""


def css(accent: str) -> str:
    return ("<style>"
            f":root{{--accent:{accent}}}"
            "*{box-sizing:border-box}"
            "body{margin:0;font:15px/1.5 'Segoe UI',system-ui,-apple-system,sans-serif;color:#1f2937;background:#f3f4f6}"
            "header{background:var(--accent);color:#fff;padding:12px 28px;display:flex;align-items:center;gap:28px;"
            "box-shadow:0 2px 6px rgba(0,0,0,.15)}"
            "header .brand{font-weight:700;font-size:18px;letter-spacing:.2px;white-space:nowrap}"
            "header nav{font-size:14px;color:rgba(255,255,255,.6)}"
            "header nav a{color:#fff;text-decoration:none;padding:6px 10px;border-radius:6px}"
            "header nav a:hover{background:rgba(255,255,255,.18)}"
            "main{max-width:980px;margin:24px auto;padding:22px 28px;background:#fff;border-radius:10px;"
            "box-shadow:0 1px 3px rgba(0,0,0,.08)}"
            "h1{font-size:22px;margin:0 0 16px;color:#111827}h2{font-size:17px;margin:22px 0 10px;color:#374151}"
            "a{color:var(--accent)}"
            "table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:14px}"
            "th{background:#f9fafb;text-align:left;font-weight:600;color:#4b5563}"
            "td,th{border-bottom:1px solid #e5e7eb;padding:8px 10px;vertical-align:top}"
            "tr:hover td{background:#fafafa}"
            "form{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-top:16px}"
            "label{display:inline-block;margin:4px 12px 8px 0;font-weight:600;color:#374151}"
            "input,select{font:inherit;padding:6px 8px;margin-left:6px;border:1px solid #d1d5db;border-radius:6px;background:#fff}"
            "button{font:inherit;font-weight:600;color:#fff;background:var(--accent);border:0;border-radius:6px;"
            "padding:8px 18px;margin-top:6px;cursor:pointer}"
            "button:hover{filter:brightness(1.1)}"
            "#error,.error{color:#b91c1c;background:#fef2f2;border:1px solid #fecaca;padding:10px 12px;border-radius:6px}"
            "#ok{color:#166534;background:#f0fdf4;border:1px solid #bbf7d0;padding:10px 12px;border-radius:6px}"
            "td[data-aiop-untrusted]{color:#4b5563;font-style:italic}"
            "</style>")


def shell(brand: str, accent: str, nav: str, title: str, body: str) -> str:
    """The whole page: escaped title in, ready HTML out (callers escape their own body)."""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>{css(accent)}</head>"
            f"<body><header><div class='brand'>{brand}</div>{nav}</header><main><h1>{title}</h1>{body}</main></body></html>")
