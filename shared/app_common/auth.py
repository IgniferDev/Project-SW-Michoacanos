from collections.abc import Iterable
from typing import Annotated

import grpc
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from proto_generated import auth_pb2
from shared.app_common.grpc_clients import grpc_channel


bearer = HTTPBearer(auto_error=False)


def require_roles(*allowed_roles: str):
    async def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> dict:
        if credentials is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
        target = request.app.state.settings.auth_grpc_target
        try:
            with grpc_channel(target) as channel:
                from proto_generated import auth_pb2_grpc

                stub = auth_pb2_grpc.AuthServiceStub(channel)
                claims = stub.ValidateToken(auth_pb2.TokenRequest(token=credentials.credentials))
        except grpc.RpcError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth service unavailable") from exc

        if not claims.valid:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        if allowed_roles and claims.role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return {
            "id": claims.user_id,
            "email": claims.email,
            "role": claims.role,
            "profile_id": claims.profile_id,
            "token": credentials.credentials,
        }

    return dependency


def is_role(user: dict, role: str) -> bool:
    return user.get("role") == role
