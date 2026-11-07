from dataclasses import dataclass


@dataclass
class GatewayError(Exception):
    code: str
    message: str
    status: int = 502
    retry_after: int | None = None

    def payload(self, request_id: str) -> dict:
        return {"error": {"code": self.code, "message": self.message, "request_id": request_id}}
