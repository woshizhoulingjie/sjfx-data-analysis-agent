import ast
import sys

for p in ("sjfx_multiuser/uploads.py", "sjfx_multiuser/upload_routes.py",
          "sjfx_multiuser/auth_routes.py", "app.py"):
    try:
        ast.parse(open(p, encoding="utf-8").read())
        print("OK   %s" % p)
    except SyntaxError as exc:
        print("FAIL %s: %r" % (p, exc))
        sys.exit(1)
print("SYNTAX OK")
