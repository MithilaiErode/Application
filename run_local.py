"""Run the app locally: loads .env and trusts the Windows certificate store (TLS inspection on this PC)."""
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))
load_dotenv(HERE / ".env")

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8010, reload=False)
