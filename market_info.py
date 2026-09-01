"""
Market information module for fetching data from Polymarket APIs.

The real lookup happens on the CLOB by condition_id: that is the reliable
source for sports markets (token id, tick size, neg risk). The Gamma API only
serves to resolve the event URL into its three markets, and it is queried by
`event_fetcher` reusing this module's HTTP client.
"""

import json
from dataclasses import dataclass
from typing import Optional

import httpx


@dataclass
class MarketData:
    """
    Container for market information.

    Only the YES token of each market is traded ("does this team win?"), so
    prices and asks are those of the YES side alone. `no_token_id` stays
    because it shares tick size and neg risk with its twin and has to be
    registered in the `order_fastpath` cache too.

    `active` and `closed` come straight from the CLOB and are what says whether
    the outcome can still be traded: the load-time filter in `event_fetcher`
    runs on the Gamma payload and its fallback can let a closed market through,
    so these are the last word on it.
    """
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    active: bool
    closed: bool
    # Required for order creation (from CLOB API)
    tick_size: str = "0.01"  # Default tick size
    neg_risk: bool = False   # Whether this is a neg risk market
    # Ask side of the book: what it COSTS to buy. `yes_price` is the best bid,
    # i.e. what you collect by selling. On illiquid markets the two values
    # diverge quite a bit, and buying while looking at the bid makes you pay
    # the spread without noticing.
    yes_ask: float = 0.0


class GammaAPIError(Exception):
    """Exception for API errors."""
    pass


class MarketClient:
    """
    Client for Polymarket APIs to fetch market metadata.

    Exposes the condition_id lookup on the CLOB and the shared HTTP client
    that `event_fetcher` uses for the Gamma API.
    """

    def __init__(
        self,
        clob_host: str = "https://clob.polymarket.com",
        gamma_host: str = "https://gamma-api.polymarket.com"
    ):
        """
        Initialize the market client.

        Args:
            clob_host: CLOB API endpoint URL
            gamma_host: Gamma API endpoint URL
        """
        self.clob_host = clob_host
        self.gamma_host = gamma_host
        # Public on purpose: event_fetcher reuses this very client for the
        # /events calls, so there is a single keep-alive connection to Gamma
        # instead of one per module.
        self.http_client = httpx.Client(timeout=10.0)

    def get_market_by_condition_id(self, condition_id: str) -> Optional[MarketData]:
        """
        Fetch market data using condition_id via CLOB API.

        The CLOB API provides reliable data including:
        - Token IDs for YES/NO outcomes
        - Current prices
        - Market status

        Args:
            condition_id: The market's condition ID (e.g., '0x1234...')

        Returns:
            MarketData object or None if not found

        Raises:
            GammaAPIError: If API request fails
        """
        url = f"{self.clob_host}/markets/{condition_id}"

        try:
            response = self.http_client.get(url)

            if response.status_code == 404:
                return None

            if response.status_code != 200:
                raise GammaAPIError(f"CLOB API returned status {response.status_code}: {response.text}")

            data = response.json()
            return self._parse_clob_market(data, condition_id)

        except httpx.RequestError as e:
            raise GammaAPIError(f"Network error: {e}")
        except json.JSONDecodeError as e:
            raise GammaAPIError(f"Invalid JSON response: {e}")

    def _parse_clob_market(self, data: dict, condition_id: str) -> Optional[MarketData]:
        """
        Parse CLOB API response into MarketData.

        CLOB API returns tokens in format:
        {
            "tokens": [
                {"token_id": "123...", "outcome": "Yes", "price": 0.45},
                {"token_id": "456...", "outcome": "No", "price": 0.55}
            ]
        }
        """
        try:
            tokens = data.get("tokens", [])

            if not tokens or len(tokens) < 2:
                return None

            # Find YES and NO tokens
            yes_token = None
            no_token = None

            for token in tokens:
                outcome = token.get("outcome", "").lower()
                if outcome == "yes":
                    yes_token = token
                elif outcome == "no":
                    no_token = token

            if not yes_token or not no_token:
                # Fallback: assume first is Yes, second is No
                yes_token = tokens[0]
                no_token = tokens[1]

            return MarketData(
                condition_id=condition_id,
                question=data.get("question", "Unknown Market"),
                yes_token_id=yes_token.get("token_id", ""),
                no_token_id=no_token.get("token_id", ""),
                yes_price=float(yes_token.get("price", 0)),
                active=data.get("active", True),
                closed=data.get("closed", False),
                tick_size=str(data.get("minimum_tick_size", "0.01")),
                neg_risk=data.get("neg_risk", False)
            )

        except (IndexError, KeyError, TypeError):
            return None

    def close(self):
        """Clean up HTTP client resources."""
        self.http_client.close()
