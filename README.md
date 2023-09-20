# intrinio-realtime-options-python-sdk
SDK for working with Intrinio's realtime options feed via WebSocket

[Intrinio](https://intrinio.com/) provides real-time stock option prices via a two-way WebSocket connection. To get started, [subscribe to a real-time data feed](https://intrinio.com/financial-market-data/options-data) and follow the instructions below.

## Requirements

- Python 3.10+
- You need https://pypi.org/project/websocket-client/, not https://pypi.org/project/websocket/.

## Installation

Go to [Release](https://github.com/intrinio/intrinio-realtime-options-python-sdk/releases/), download the zip, extract client.py, and reference it in your project.

## Sample Project

For a sample Python application see: [intrinio-realtime-options-python-sdk](https://github.com/intrinio/intrinio-realtime-options-python-sdk/blob/main/example_app.py)

## Features

* Receive streaming, real-time option price updates:
	* every trade
	* conflated bid and ask
	* open interest, open, close, high, low
	* unusual activity(block trades, sweeps, whale trades (large), unusual sweep trades)
* Subscribe to updates from individual options contracts (or option chains)
* Subscribe to updates for the entire univers of option contracts (~1.5M option contracts)

## Example Usage

```python
import threading
import signal
import time
import sys
from threading import Event, Lock
import client

trade_count = 0
trade_count_lock = Lock()
quote_count = 0
quote_count_lock = Lock()
refresh_count = 0
refresh_count_lock = Lock()
block_count = 0
block_count_lock = Lock()
sweep_count = 0
sweep_count_lock = Lock()
large_trade_count = 0
large_trade_count_lock = Lock()
unusual_sweep_count = 0
unusual_sweep_count_lock = Lock()


def on_quote(quote: client.Quote):
    global quote_count
    global quote_count_lock
    with quote_count_lock:
        quote_count += 1


def on_trade(trade: client.Trade):
    global trade_count
    global trade_count_lock
    with trade_count_lock:
        trade_count += 1


def on_refresh(refresh: client.Refresh):
    global refresh_count
    global refresh_count_lock
    with refresh_count_lock:
        refresh_count += 1


def on_unusual_activity(ua: client.UnusualActivity):
    global block_count
    global block_count_lock
    global sweep_count
    global sweep_count_lock
    global large_trade_count
    global large_trade_count_lock
    global unusual_sweep_count
    global unusual_sweep_count_lock
    if ua.activity_type == client.UnusualActivityType.BLOCK:
        with block_count_lock:
            block_count += 1
    elif ua.activity_type == client.UnusualActivityType.SWEEP:
        with sweep_count_lock:
            sweep_count += 1
    elif ua.activity_type == client.UnusualActivityType.LARGE:
        with large_trade_count_lock:
            large_trade_count += 1
    elif ua.activity_type == client.UnusualActivityType.UNUSUAL_SWEEP:
        with unusual_sweep_count_lock:
            unusual_sweep_count += 1
    else:
        client.log("on_unusual_activity - Unknown activity_type {0}", ua.activity_type)


class Summarize(threading.Thread):
    def __init__(self, stop_flag: threading.Event, intrinio_client: client.Client):
        threading.Thread.__init__(self, args=(), kwargs=None, daemon=True)
        self.__stop_flag: threading.Event = stop_flag
        self.__client = intrinio_client

    def run(self):
        while not self.__stop_flag.is_set():
            time.sleep(10.0)
            (dataMsgs, txtMsgs, queueDepth) = self.__client.get_stats()
            client.log("Client Stats - Data Messages: {0}, Text Messages: {1}, Queue Depth: {2}".format(dataMsgs, txtMsgs, queueDepth))
            client.log(
                "App Stats - Trades: {0}, Quotes: {1}, Refreshes: {2}, Blocks: {3}, Sweeps: {4}, Large Trades: {5}, Unusual Sweeps: {6}"
                .format(
                    trade_count,
                    quote_count,
                    refresh_count,
                    block_count,
                    sweep_count,
                    large_trade_count,
                    unusual_sweep_count))


# Your config object MUST include the 'api_key' and 'provider', at a minimum
config: client.Config = client.Config(
    api_key="",
    provider=client.Providers.OPRA,
    num_threads=8,
    symbols=["AAPL"],
    # this is a static list of symbols (options contracts or option chains) that will automatically be subscribed to when the client starts
    log_level=client.LogLevel.INFO)

# Register only the callbacks that you want.
# Take special care when registering the 'on_quote' handler as it will increase throughput by ~10x
intrinioRealtimeOptionsClient: client.Client = client.Client(config, on_trade=on_trade, on_quote=on_quote, on_refresh=on_refresh, on_unusual_activity=on_unusual_activity)

# Use this to subscribe to the entire universe of symbols (option contracts). This requires special permission.
# intrinioRealtimeOptionsClient.join_firehose()

# Use this to subscribe, dynamically, to an option chain (all option contracts for a given underlying contract).
# intrinioRealtimeOptionsClient.join("AAPL")

# Use this to subscribe, dynamically, to a specific option contract.
# intrinioRealtimeOptionsClient.join("AAP___230616P00250000")

# Use this to subscribe, dynamically, a list of specific option contracts or option chains.
# intrinioRealtimeOptionsClient.join("GOOG__220408C02870000", "MSFT__220408C00315000", "AAPL__220414C00180000", "TSLA", "GE")

stop_event = Event()


def on_kill_process(sig, frame):
    client.log("Sample Application - Stopping")
    stop_event.set()
    intrinioRealtimeOptionsClient.stop()
    sys.exit(0)


signal.signal(signal.SIGINT, on_kill_process)

summarize_thread = Summarize(stop_event, intrinioRealtimeOptionsClient)
summarize_thread.start()

intrinioRealtimeOptionsClient.start()

time.sleep(60 * 60)
# sigint, or ctrl+c, during the thread wait will also perform the same below code.
on_kill_process(None, None)
```

## Handling Quotes

There are millions of options contracts, each with their own feed of activity.
We highly encourage you to make your onTrade, onQuote, onUnusualActivity, and onRefresh methods as short as possible and follow a queue pattern so your app can handle the large volume of activity.
Note that quotes (ask and bid updates) comprise 99% of the volume of the entire feed. Be cautious when deciding to receive quote updates.

## Providers

Currently, Intrinio offers realtime data for this SDK from the following providers:

* OPRA - [Homepage](https://www.opraplan.com/)


## Data Format

### Trade Message

```python
class Trade:
    def __init__(self, contract: str, exchange: Exchange, price: float, size: int, timestamp: float, total_volume: int, qualifiers: tuple, ask_price_at_execution: float, bid_price_at_execution: float, underlying_price_at_execution: float):
        self.contract: str = contract
        self.exchange: Exchange = exchange
        self.price: float = price
        self.size: int = size
        self.timestamp: float = timestamp
        self.total_volume: int = total_volume
        self.qualifiers: tuple = qualifiers
        self.ask_price_at_execution = ask_price_at_execution
        self.bid_price_at_execution = bid_price_at_execution
        self.underlying_price_at_execution = underlying_price_at_execution
```

* **contract** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **exchange** - Exchange(IntEnum): the specific exchange through which the trade occurred
* **price** - the price in USD
* **size** - the size of the last trade in hundreds (each contract is for 100 shares).
* **total_volume** - The number of contracts traded so far today.
* **timestamp** - a Unix timestamp (with microsecond precision)
* **qualifiers** - a tuple containing 4 ints: each item represents one trade qualifier. see list of possible [Trade Qualifiers](#Trade Qualifiers), below. 
* **ask_price_at_execution** - the contract ask price in USD at the time of execution.
* **bid_price_at_execution** - the contract bid price in USD at the time of execution.
* **underlying_price_at_execution** - the contract's underlying security price in USD at the time of execution.

### Trade Qualifiers

The trade qualifiers field is represented by a tuple containing 4 integers. Each integer can take one of the following values:
    **0**: Regular transaction
    **2**: Cancel
    **3**: This is the last price and it's cancelled
    **4**: Late but in sequence / sold last late
    **5**: This was the open price and it's cancelled
    **6**: Late report of opening trade and is out of sequence: or set the open
    **7**: Cancel only trade reported
    **8**: Transaction was executed electronically
    **9**: Reopen of a previously halted contract
    **11**: Spread
    **23**: Intermarket Sweep
    **30**: Extended hours
    **33**: Crossed trade including Request For Cross RFC
    **87**: Complex trade with equity leg
    **107**: Auction
    **123**: Stock option trade
    **136**: Ex-Pit trade
    **192**: Message received locally out-of-sequence
    **222**: Combo trade
    **0**: Blank

Each trade can be qualified by a maximum of 4(four) values. The combination of these values can have special values. These special values are:
    **107, 23**: Single leg auction ISO
    **23, 33**: Single leg cross ISO
    **8, 11**: Multi leg auto-electronic trade
    **107, 11**: Multi leg auction
    **11, 33**: Multi leg cross
    **136, 11**: Multi leg floor trade
    **8, 11, 87**: Multi leg auto-electronic trade against single leg(s)
    **107, 123**: Stock options auction
    **107, 11, 87**: Multi leg auction against single leg(s)
    **136, 11, 87**: Multi leg floor trade against single leg(s)
    **8, 123**: Stock options auto-electronic trade
    **123, 33**: Stock options cross
    **136, 123**: Stock options floor trade
    **8, 87, 123**: Stock options auto-electronic trade against single leg(s)
    **107, 87, 123**: Stock options auction against single leg(s)
    **136, 87, 123**: Stock options floor trade against single leg(s)
    **136, 11, 222**: Multi leg floor trade of proprietary products
    **222, 30**: Multilateral Compression Trade of Proprietary Data Products

### Quote Message

```python
class Quote:
    def __init__(self, contract: str, ask_price: float, ask_size: int, bid_price: float, bid_size: int, timestamp: float):
        self.contract: str = contract
        self.ask_price: float = ask_price
        self.bid_price: float = bid_price
        self.ask_size: int = ask_size
        self.bid_size: int = bid_size
        self.timestamp: float = timestamp
```

* **contract** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **ask_price** - the ask price in USD
* **ask_size** - the size of the last ask in hundreds (each contract is for 100 shares).
* **bid_price** - the bid price in USD
* **bid_size** - the size of the last bid in hundreds (each contract is for 100 shares).
* **timestamp** - a Unix timestamp (with microsecond precision)


### Refresh Message

```python
class Refresh:
    def __init__(self, contract: str, open_interest: int, open_price: float, close_price: float, high_price: float, low_price: float):
        self.contract: str = contract
        self.open_interest: int = open_interest
        self.open_price: float = open_price
        self.close_price: float = close_price
        self.high_price: float = high_price
        self.low_price: float = low_price
```

* **contract** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **openInterest** - the total quantity of opened contracts as reported at the start of the trading day
* **open_price** - the open price in USD
* **close_price** - the close price in USD
* **high_price** - the daily high price in USD
* **low_price** - the daily low price in USD

### Unusual Activity Message
```python
class UnusualActivity:
    def __init__(self,
                 contract: str,
                 activity_type: UnusualActivityType,
                 sentiment: UnusualActivitySentiment,
                 total_value: float,
                 total_size: int,
                 average_price: float,
                 ask_price_at_execution: float,
                 bid_price_at_execution: float,
                 underlying_price_at_execution: float,
                 timestamp: float):
        self.contract: str = contract
        self.activity_type: UnusualActivityType = activity_type
        self.sentiment: UnusualActivitySentiment = sentiment
        self.total_value: float = total_value
        self.total_size: int = total_size
        self.average_price: float = average_price
        self.ask_price_at_execution: float = ask_price_at_execution
        self.bid_price_at_execution: float = bid_price_at_execution
        self.underlying_price_at_execution: float = underlying_price_at_execution
        self.timestamp: float = timestamp
```

* **contract** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **activity_type** - The type of unusual activity that was detected
  * **`Block`** - represents an 'block' trade
  * **`Sweep`** - represents an intermarket sweep
  * **`Large`** - represents a trade of at least $100,000
  * **`UnusualSweep`** - represents an unusually large sweep near market open
* **sentiment** - The sentiment of the unusual activity event
  *    **`Neutral`** - Reflects a minimal expected price change
  *    **`Bullish`** - Reflects an expected positive (upward) change in price
  *    **`Bearish`** - Reflects an expected negative (downward) change in price
* **total_value** - The total value of the trade in USD. 'Sweeps' and 'blocks' can be comprised of multiple trades. This is the value of the entire event.
* **total_size** - The total size of the trade in number of contracts. 'Sweeps' and 'blocks' can be comprised of multiple trades. This is the total number of contracts exchanged during the event.
* **average_price** - The average price at which the trade was executed. 'Sweeps' and 'blocks' can be comprised of multiple trades. This is the average trade price for the entire event.
* **ask_price_at_execution** - The 'ask' price of the underlying at execution of the trade event.
* **bid_price_at_execution** - The 'bid' price of the underlying at execution of the trade event.
* **underlying_price_at_execution** - The last trade price of the underlying at execution of the trade event.
* **Timestamp** - a Unix timestamp (with microsecond precision).

## API Keys

You will receive your Intrinio API Key after [creating an account](https://intrinio.com/signup). You will need a subscription to a [realtime data feed](https://intrinio.com/financial-market-data/options-data) as well.

## Documentation

### Overview

The Intrinio Realtime Client will handle authorization as well as establishment and management of all necessary WebSocket connections. All you need to get started is your API key.
The first thing that you'll do is create a new `Client` object, passing in a series of callbacks. These callback methods tell the client what types of subscriptions you will be setting up.
After a `Client` object has been created, you may subscribe to receive feed updates from the server.
You may subscribe to static list of symbols (a mixed list of option contracts and/or option chains). 
Or, you may subscribe, dynamically, to option contracts, option chains, or a mixed list thereof.
It is also possible to subscribe to the entire universe of option contracts by switching the `Provider` to "OPRA_FIREHOSE" (in the config object) and calling `join_firehose`.
The volume of data provided by the `Firehose` exceeds 100Mbps and requires special authorization.
After subscribing, using your starting list of symbols, you will call the `start` method. The client will immediately attempt to authorize your API key (provided in the config object). If authoriztion is successful, the necessary connection(s) will be opened.
If you are using the non-firehose feed, you may update your subscriptions on the fly, using the `join` and `leave` methods.
The WebSocket client is designed for near-indefinite operation. It will automatically reconnect if a connection drops/fails and when then servers turn on every morning.
If you wish to perform a graceful shutdown of the application, please call the `stop` method.

### Methods

`client : Client = Client(config : Config, on_trade : Callable[[Trade], None], on_quote : Callable[[Quote], None] = None, on_refresh : Callable[[Refresh], None] = None, on_unusual_activity : Callable[[UnusualActivity],None] = None)` - Creates an Intrinio Real-Time client.
* **Parameter** `config`: The configuration to be used by the client.
* **Parameter** `on_trade`: The Callable accepting trades. If no `on_trade` callback is provided, you will not receive trade updates from the server.
* **Parameter** `on_quote`: The Callable accepting quotes. If no `on_quote` callback is provided, you will not receive quote (ask, bid) updates from the server.
* **Parameter** `on_refresh`: The Callable accepting refresh messages. If no `on_refresh` callback is provided, you will not receive open interest, high, low, open, or close data from the server. Note: open interest data is only updated at the beginning of every trading day. If this callback is provided you will recieve an update immediately, as well as every 15 minutes (approx).
* **Parameter** `on_unusual_activity`: The Callable accepting unusual activity events. If no `on_unusual_activity` callback is provided, you will not receive unusual activity updates from the server.

---------

`client.start()` - Starts the Intrinio Realtime WebSocket Client.
	This method will immediately attempt to authorize the API key (provided in config).
	After successful authorization, all of the data processing threads will be started, and the websocket connections will be opened.
	If a subscription has already been created with one of the `join` methods, data will begin to flow.

---------

`client.join()` - Joins channel(s) configured in config.json.
`client.join(*channels)` - Joins the provided channel or channels. E.g. "AAPL", "GOOG__210917C01040000"
`client.join_firehose()` - Joins the firehose channel. This requires special account permissions.

---------

`client.leave()` - Leaves all joined channels/subscriptions, including firehose.
`client.leave(*channels)` - Leaves the specified channel or channels. E.g. "AAPL" or "GOOG__210917C01040000"
`client.leave_firehose()` Leaves the firehose channel 

---------
`client.stop();` - Stops the Intrinio Realtime WebSocket Client. This method will leave all joined channels, stop all threads, and gracefully close the websocket connection(s).


## Configuration

### config.json
```python
class Config:
    def __init__(self, apiKey : str, provider : Providers, numThreads : int = 4, logLevel : LogLevel = LogLevel.INFO, manualIpAddress : str = None, symbols : set[str] = None):
        self.apiKey : str = apiKey
        self.provider : Providers = provider # Providers.OPRA or Providers.MANUAL
        self.numThreads : int = numThreads # At least 4 threads are recommended for 'FIREHOSE' connections
        self.manualIpAddress : str = manualIpAddress
        self.symbols : list[str] = symbols # Static list of symbols to use
        self.logLevel : LogLevel = logLevel
```