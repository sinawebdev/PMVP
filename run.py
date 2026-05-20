import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = os.getenv("PORT", "5000")
    if os.getenv("RENDER") == "true":
        os.execvp("gunicorn", ["gunicorn", "run:app", "--bind", f"0.0.0.0:{port}"])

    app.run(
        host="0.0.0.0",
        port=int(port),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )
