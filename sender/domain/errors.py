class ApiError(Exception):
    def __init__(self, service: str, status: int | None, message: str, payload: dict | None = None) -> None:
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{service} error{detail}: {message}")
        self.service = service
        self.status = status
        self.payload = payload or {}


class DaftraApiError(ApiError):
    def __init__(self, status: int | None, message: str, payload: dict | None = None) -> None:
        super().__init__("Daftra", status, message, payload)


class WhatsAppApiError(ApiError):
    def __init__(self, status: int | None, message: str, payload: dict | None = None) -> None:
        super().__init__("WhatsApp", status, message, payload)