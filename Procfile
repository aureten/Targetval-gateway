web: gunicorn app.main:app -k uvicorn.workers.UvicornWorker --workers 2 --timeout 120 --keep-alive 10 --access-logfile - --error-logfile -
