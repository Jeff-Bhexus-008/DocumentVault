"""Doxari entry point.

Gunicorn runs `app:app` exactly as before, so the Render service configuration
does not need to change.
"""

import os

from doxari import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
