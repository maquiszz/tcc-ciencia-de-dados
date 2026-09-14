"""Ponto de entrada único do Spa Panaceia.

Hospedagens que procuram automaticamente por ``app:app`` recebem exatamente a
mesma aplicação usada no desenvolvimento e no comando do Procfile.
"""

from database.cadastro_interface import app

if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
