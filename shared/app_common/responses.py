def ok(data=None, message: str = "") -> dict:
    return {"success": True, "data": data, "message": message}


def fail(message: str, data=None) -> dict:
    return {"success": False, "data": data, "message": message}
