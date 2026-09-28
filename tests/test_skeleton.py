def test_imports() -> None:
    from patch_engine import (  # noqa: F401
        api,
        engine,
        journal,
        model,
        parser,
        service,
        store,
    )
