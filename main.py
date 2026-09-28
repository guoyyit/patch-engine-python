import os

from patch_engine.api import create_app

app = create_app(os.environ.get("PATCH_ENGINE_WORKSPACE", "workspace"))
