import os

import uvicorn

if __name__ == "__main__":
    # 0.0.0.0 so the Expo mobile app on the same network can reach the API;
    # set HOST=127.0.0.1 to keep it local-only.
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
