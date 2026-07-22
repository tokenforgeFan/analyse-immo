"""
serveur.py  -  Lance le site en mode PRODUCTION (pas le serveur de dev Flask).

Le serveur integre de Flask n'est pas prevu pour du public : lent, mono-tache,
et il expose un debugger dangereux. Ici on utilise Waitress (fonctionne sous
Windows ET Linux, sans dependance systeme).

Installation :
    pip install waitress

Lancement :
    python serveur.py                 # port 8000
    set PORT=80 && python serveur.py  # autre port (Windows)
"""

import os
from waitress import serve
from app import app, PLAYWRIGHT_DISPO, BASE_DUCKDB, duckdb

PORT = int(os.environ.get("PORT", 8000))
THREADS = int(os.environ.get("THREADS", 8))

if __name__ == "__main__":
    base = "base DuckDB" if (duckdb and os.path.exists(BASE_DUCKDB)) else "CSV a la demande"
    nav = "Playwright actif" if PLAYWRIGHT_DISPO else "sans Playwright"
    print(f"\n  PRODUCTION - {base}, {nav}")
    print(f"  Ecoute sur le port {PORT} ({THREADS} threads)")
    print(f"  Local : http://localhost:{PORT}\n")
    serve(app, host="0.0.0.0", port=PORT, threads=THREADS)
