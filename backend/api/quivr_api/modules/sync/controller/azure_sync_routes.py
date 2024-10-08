import os

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from msal import ConfidentialClientApplication

from quivr_api.logger import get_logger
from quivr_api.middlewares.auth import AuthBearer, get_current_user
from quivr_api.modules.sync.dto.inputs import (
    SyncsUserInput,
    SyncsUserStatus,
    SyncUserUpdateInput,
)
from quivr_api.modules.sync.service.sync_service import SyncService, SyncUserService
from quivr_api.modules.user.entity.user_identity import UserIdentity

from .successfull_connection import successfullConnectionPage

# Initialize logger
logger = get_logger(__name__)

# Initialize sync service
sync_service = SyncService()
sync_user_service = SyncUserService()

# Initialize API router
azure_sync_router = APIRouter()

# Constants
CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID")
CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET")
AUTHORITY = "https://login.microsoftonline.com/common"
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:5050")
REDIRECT_URI = f"{BACKEND_URL}/sync/azure/oauth2callback"
SCOPE = [
    "https://graph.microsoft.com/Files.Read",
    "https://graph.microsoft.com/User.Read",
    "https://graph.microsoft.com/Sites.Read.All",
]


@azure_sync_router.post(
    "/sync/azure/authorize",
    dependencies=[Depends(AuthBearer())],
    tags=["Sync"],
)
def authorize_azure(
    request: Request, name: str, current_user: UserIdentity = Depends(get_current_user)
):
    """
    Authorize Azure sync for the current user.

    Args:
        request (Request): The request object.
        current_user (UserIdentity): The current authenticated user.

    Returns:
        dict: A dictionary containing the authorization URL.
    """
    client = ConfidentialClientApplication(
        CLIENT_ID, client_credential=CLIENT_SECRET, authority=AUTHORITY
    )
    logger.debug(f"Authorizing Azure sync for user: {current_user.id}")
    state = f"user_id={current_user.id}, name={name}"
    flow = client.initiate_auth_code_flow(
        scopes=SCOPE, redirect_uri=REDIRECT_URI, state=state, prompt="select_account"
    )

    sync_user_input = SyncsUserInput(
        user_id=str(current_user.id),
        name=name,
        provider="Azure",
        credentials={},
        state={"state": state},
        additional_data={"flow": flow},
        status=str(SyncsUserStatus.SYNCING),
    )
    sync_user_service.create_sync_user(sync_user_input)
    return {"authorization_url": flow["auth_uri"]}


@azure_sync_router.get("/sync/azure/oauth2callback", tags=["Sync"])
def oauth2callback_azure(request: Request):
    """
    Handle OAuth2 callback from Azure.

    Args:
        request (Request): The request object.

    Returns:
        dict: A dictionary containing a success message.
    """
    client = ConfidentialClientApplication(
        CLIENT_ID, client_credential=CLIENT_SECRET, authority=AUTHORITY
    )
    state = request.query_params.get("state")
    state_split = state.split(",")
    current_user = state_split[0].split("=")[1]  # Extract user_id from state
    name = state_split[1].split("=")[1] if state else None
    state_dict = {"state": state}
    logger.debug(
        f"Handling OAuth2 callback for user: {current_user} with state: {state}"
    )
    sync_user_state = sync_user_service.get_sync_user_by_state(state_dict)
    logger.info(f"Retrieved sync user state: {sync_user_state}")

    if not sync_user_state or state_dict != sync_user_state.state:
        logger.error("Invalid state parameter")
        raise HTTPException(status_code=400, detail="Invalid state parameter")
    if str(sync_user_state.user_id) != current_user:
        logger.info(f"Sync user state: {sync_user_state}")
        logger.info(f"Current user: {current_user}")
        logger.info(f"Sync user state user_id: {sync_user_state.user_id}")
        logger.error("Invalid user")
        raise HTTPException(status_code=400, detail="Invalid user")

    result = client.acquire_token_by_auth_code_flow(
        sync_user_state.additional_data["flow"], dict(request.query_params)
    )
    if "access_token" not in result:
        logger.error(f"Failed to acquire token: {result}")
        raise HTTPException(
            status_code=400,
            detail=f"Failed to acquire token: {result}",
        )

    access_token = result["access_token"]

    creds = result
    logger.info(f"Fetched OAuth2 token for user: {current_user}")

    # Fetch user email from Microsoft Graph API
    graph_url = "https://graph.microsoft.com/v1.0/me"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(graph_url, headers=headers)
    if response.status_code != 200:
        logger.error("Failed to fetch user profile from Microsoft Graph API")
        raise HTTPException(status_code=400, detail="Failed to fetch user profile")

    user_info = response.json()
    user_email = user_info.get("mail") or user_info.get("userPrincipalName")
    logger.info(f"Retrieved email for user: {current_user} - {user_email}")

    sync_user_input = SyncUserUpdateInput(
        credentials=result,
        email=user_email,
        status=str(SyncsUserStatus.SYNCED),
    )

    sync_user_service.update_sync_user(current_user, state_dict, sync_user_input)
    logger.info(f"Azure sync created successfully for user: {current_user}")
    return HTMLResponse(successfullConnectionPage)
