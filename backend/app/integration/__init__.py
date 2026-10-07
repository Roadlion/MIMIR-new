try:
    from .plugin import MIMIRPlugin
except Exception:
    try:
        from app.integration.plugin import MIMIRPlugin
    except Exception:
        MIMIRPlugin = None