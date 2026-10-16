"""Bounded bodies for cached account-selection reads; these never refresh providers."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .product_connections import MAX_SAVED_CONNECTIONS
from .snaptrade_client import MAX_ACCOUNTS

MAX_SELECTED_ACCOUNTS = (MAX_SAVED_CONNECTIONS + 1) * MAX_ACCOUNTS
AccountId = Annotated[str, Field(strict=True, min_length=1, max_length=240)]


class AccountSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account: list[AccountId] = Field(max_length=MAX_SELECTED_ACCOUNTS)


class WealthAccountSelection(AccountSelection):
    # Wealth adds a source namespace, matching its existing source-ID contract.
    account: list[Annotated[str, Field(strict=True, min_length=1, max_length=300)]] = Field(max_length=MAX_SELECTED_ACCOUNTS)
