# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""
This module implements a tool which prepares a transaction for the transaction settlement skill.
Please note that the gnosis safe parameters are missing from the payload, e.g., `safe_tx_hash`, `safe_tx_gas`, etc.
"""
import functools
import os
import traceback
from typing import Any, Dict, Optional, Tuple, Callable

import anthropic
import googleapiclient
import openai
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI
from openai import OpenAI
from prediction_market_agent_tooling.markets.agent_market import AgentMarket
from prediction_market_agent_tooling.markets.omen.omen import OmenAgentMarket
from prediction_market_agent_tooling.markets.omen.omen_contracts import (
    OmenFixedProductMarketMakerContract,
    OmenCollateralTokenContract,
)
from prediction_market_agent_tooling.tools.utils import check_not_none
from prediction_market_agent_tooling.tools.web3_utils import (
    prepare_tx,
)
from pydantic import BaseModel
from web3 import Web3
from web3.types import TxParams

MechResponse = Tuple[str, Optional[str], Optional[Dict[str, Any]], Any, Any]


def with_key_rotation(func: Callable):
    @functools.wraps(func)
    def wrapper(*args, **kwargs) -> MechResponse:
        # this is expected to be a KeyChain object,
        # although it is not explicitly typed as such
        api_keys = kwargs["api_keys"]
        retries_left: Dict[str, int] = api_keys.max_retries()

        def execute() -> MechResponse:
            """Retry the function with a new key."""
            try:
                result = func(*args, **kwargs)
                return result + (api_keys,)
            except anthropic.RateLimitError as e:
                # try with a new key again
                service = "anthropic"
                if retries_left[service] <= 0:
                    raise e
                retries_left[service] -= 1
                api_keys.rotate(service)
                return execute()
            except openai.RateLimitError as e:
                # try with a new key again
                if retries_left["openai"] <= 0 and retries_left["openrouter"] <= 0:
                    raise e
                retries_left["openai"] -= 1
                retries_left["openrouter"] -= 1
                api_keys.rotate("openai")
                api_keys.rotate("openrouter")
                return execute()
            except googleapiclient.errors.HttpError as e:
                # try with a new key again
                rate_limit_exceeded_code = 429
                if e.status_code != rate_limit_exceeded_code:
                    raise e
                service = "google_api_key"
                if retries_left[service] <= 0:
                    raise e
                api_keys.rotate(service)
                return execute()
            except Exception as e:
                return str(e), "", None, None, api_keys

        mech_response = execute()
        return mech_response

    return wrapper


ENGINE = "gpt-3.5-turbo"
MAX_TOKENS = 500
TEMPERATURE = 0.7


"""NOTE: An LLM is used for generating a dict containing interpreted parameters from the response, such as "recipient_address", "market_address", etc. This could also be done if we could somehow publish the parameters needed by the run method and make it discoverable by the caller."""

BUY_YES_TOKENS_PROMPT = """You are an LLM inside a multi-agent system that takes in a prompt from a user requesting you to produce transaction parameters which
will later be part of an Ethereum transaction.
Interpret the USER_PROMPT and extract the required information.
Do not use any functions.

[USER_PROMPT]
{user_prompt}

Follow the formatting instructions below for producing an output in the correct format.
{format_instructions}
"""

client: Optional[OpenAI] = None


class BuyYesParams(BaseModel):
    sender: str
    market_id: str
    outcome: bool
    amount_to_buy: float


def build_buy_params_from_prompt(user_prompt: str) -> BuyYesParams:
    model = ChatOpenAI(temperature=0)
    parser = PydanticOutputParser(pydantic_object=BuyYesParams)

    prompt = PromptTemplate(
        template=BUY_YES_TOKENS_PROMPT,
        input_variables=["user_prompt"],
        partial_variables={"format_instructions": parser.get_format_instructions()},
    )

    chain = prompt | model | parser

    return chain.invoke({"user_prompt": user_prompt})


class OpenAIClientManager:
    """Client context manager for OpenAI."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    def __enter__(self) -> OpenAI:
        global client
        if client is None:
            client = OpenAI(api_key=self.api_key)
        return client

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        global client
        if client is not None:
            client.close()
            client = None


def build_approval_tx_params(
    buy_params: BuyYesParams, market: AgentMarket, w3: Web3
) -> TxParams:
    """
    # Approve the market maker to withdraw our collateral token.
    """
    from_address_checksummed = Web3.to_checksum_address(buy_params.sender)
    amount_wei = Web3.to_wei(buy_params.amount_to_buy, "ether")

    market_contract: OmenFixedProductMarketMakerContract = market.get_contract()
    collateral_token_contract = OmenCollateralTokenContract()

    tx_params_approve = prepare_tx(
        web3=w3,
        contract_address=collateral_token_contract.address,
        contract_abi=collateral_token_contract.abi,
        from_address=from_address_checksummed,
        function_name="approve",
        function_params=[
            market_contract.address,
            amount_wei,
        ],
    )
    return tx_params_approve


def check_balance_sufficient_for_buying_token(
    buy_params: BuyYesParams, w3: Web3
) -> bool:
    # First, check if user has enough collateral to place bet. If not, abort.
    # We restrict our bets to xDAI-denominated amounts for simplicity.
    amount_wei = Web3.to_wei(buy_params.amount_to_buy, "ether")

    collateral_token_contract = OmenCollateralTokenContract()

    collateral_token_balance_wei = collateral_token_contract.balanceOf(
        for_address=Web3.to_checksum_address(buy_params.sender), web3=w3
    )
    # Caller needs to make sure he has enough wxDAI (> amount_wei), else it should fail
    if collateral_token_balance_wei < amount_wei:
        return False
    return True


def build_buy_tokens_tx_params(
    buy_params: BuyYesParams, market: AgentMarket, w3: Web3
) -> TxParams:

    from_address_checksummed = Web3.to_checksum_address(buy_params.sender)
    amount_wei = Web3.to_wei(buy_params.amount_to_buy, "ether")

    market_contract: OmenFixedProductMarketMakerContract = market.get_contract()

    # Get the index of the outcome we want to buy.
    outcome_str = "True" if buy_params.outcome else "False"
    outcome_index: int = market.get_outcome_index(outcome_str)

    # Allow 1% slippage.
    expected_shares = market_contract.calcBuyAmount(amount_wei, outcome_index, web3=w3)

    # Buy shares using the deposited xDai in the collateral token.
    tx_params_buy = prepare_tx(
        web3=w3,
        contract_address=market_contract.address,
        contract_abi=market_contract.abi,
        from_address=from_address_checksummed,
        function_name="buy",
        function_params=[
            amount_wei,
            outcome_index,
            expected_shares,
        ],
    )
    return tx_params_buy


def build_buy_yes_tx(
    prompt: str,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]], Any]:
    """Perform native transfer."""
    tool_prompt = BUY_YES_TOKENS_PROMPT.format(user_prompt=prompt)
    try:
        # parse the response to get the transaction object string itself
        # parsed_txs = ast.literal_eval(response)
        buy_params = build_buy_params_from_prompt(user_prompt=tool_prompt)

        # Calculate the amount of shares we will get for the given investment amount.
        # ToDo - Clarify how to use RPC inside mech
        GNOSIS_RPC_URL = check_not_none(os.environ["GNOSIS_RPC_URL"])
        if not GNOSIS_RPC_URL:
            raise EnvironmentError("GNOSIS_RPC_URL not set. Aborting.")
        w3 = Web3(Web3.HTTPProvider(GNOSIS_RPC_URL))

        market: AgentMarket = OmenAgentMarket.get_binary_market(buy_params.market_id)

        tx_params_approve = build_approval_tx_params(
            buy_params=buy_params, market=market, w3=w3
        )
        tx_params_buy = build_buy_tokens_tx_params(
            buy_params=buy_params, market=market, w3=w3
        )

        # We return the transactions_dict below in order to be able to return multiple transactions for later execution instead of just one.
        transaction_dict = {}
        transaction_dict["1"] = tx_params_approve
        transaction_dict["2"] = tx_params_buy

        return "", prompt, transaction_dict, None

    except Exception as e:
        traceback.print_exception(e)
        return f"exception occurred - {e}", None, None, None


AVAILABLE_TOOLS = {
    "buy_yes_omen": build_buy_yes_tx,
}


def error_response(msg: str) -> Tuple[str, None, None, None]:
    """Return an error mech response."""
    return msg, None, None, None


@with_key_rotation
def run(**kwargs) -> Tuple[str, Optional[str], Optional[Dict[str, Any]], Any]:
    """Run the task"""
    tool: str | None = kwargs.get("tool", None)

    if tool is None:
        return error_response("No tool has been specified.")

    prompt: str | None = kwargs.get("prompt", None)
    if prompt is None:
        return error_response("No prompt has been given.")

    transaction_builder = AVAILABLE_TOOLS.get(tool)
    if transaction_builder is None:
        return error_response(
            f"Tool {tool!r} is not in supported tools: {tuple(AVAILABLE_TOOLS.keys())}."
        )

    api_key: str | None = kwargs.get("api_keys", {}).get("openai", None)
    if api_key is None:
        return error_response("No api key has been given.")

    with OpenAIClientManager(api_key):
        return transaction_builder(prompt)
