# intrinio-realtime-options-python-sdk
SDK for working with Intrinio's realtime options feed via WebSocket

[Intrinio](https://intrinio.com/) provides real-time stock option prices via a two-way WebSocket connection. To get started, [subscribe to a real-time data feed](https://intrinio.com/financial-market-data/options-data) and follow the instructions below.

## Requirements

- Python 3.10+

## Installation

Go to [Release](https://github.com/intrinio/intrinio-realtime-options-python-sdk/releases/), download the zip, extract client.py, and reference it in your project.

## Sample Project

For a sample Python application see: [intrinio-realtime-options-python-sdk](https://github.com/intrinio/intrinio-realtime-options-python-sdk/blob/main/example_app.py)

## Features

* Receive streaming, real-time option price updates:
	* every trade
	* conflated bid and ask
	* open interest
	* unusual activity(block trades, sweeps, whale trades)
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
ask_count = 0
ask_count_lock = Lock()
bid_count = 0
bid_count_lock = Lock()
refresh_count = 0
refresh_count_lock = Lock()
block_count = 0
block_count_lock = Lock()
sweep_count = 0
sweep_count_lock = Lock()
large_trade_count = 0
large_trade_count_lock = Lock()


def on_quote(quote: client.Quote):
    global ask_count
    global ask_count_lock
    global bid_count
    global bid_count_lock
    if (quote.type == client.QuoteType.ASK):
        with ask_count_lock:
            ask_count += 1
    elif (quote.type == client.QuoteType.BID):
        with bid_count_lock:
            bid_count += 1
    else:
        client.log("on_quote - Unknown quote activity_type {0}", quote.type)


def on_trade(trade: client.Trade):
    global trade_count
    global trade_count_lock
    with trade_count_lock: trade_count += 1


def on_refresh(oi: client.Refresh):
    global refresh_count
    global refresh_count_lock
    with open_interest_count_lock: open_interest_count += 1


def on_unusual_activity(ua: client.UnusualActivity):
    global block_count
    global block_count_lock
    global sweep_count
    global sweep_count_lock
    global large_trade_count
    global large_trade_count_lock
    if (ua.activity_type == client.UnusualActivityType.BLOCK):
        with block_count_lock:
            block_count += 1
    elif (ua.activity_type == client.UnusualActivityType.SWEEP):
        with sweep_count_lock:
            sweep_count += 1
    elif (ua.activity_type == client.UnusualActivityType.LARGE):
        with large_trade_count_lock:
            large_trade_count += 1
    else:
        client.log("on_unusual_activity - Unknown activity_type {0}", ua.activity_type)


class Summarize(threading.Thread):
    def __init__(self, stop_flag: threading.Event, client: client.Client):
        threading.Thread.__init__(self, args=(), kwargs=None, daemon=True)
        self.__stop_flag: threading.Event = stop_flag
        self.__client = client

    def run(self):
        while (not self.__stop_flag.is_set()):
            time.sleep(10.0)
            (dataMsgs, txtMsgs, queueDepth) = self.__client.getStats()
            client.log(
                "Client Stats - Data Messages: {0}, Text Messages: {1}, Queue Depth: {2}".format(dataMsgs, txtMsgs,
                                                                                                 queueDepth))
            client.log(
                "App Stats - Trades: {0}, Asks: {1}, Bids: {2}, Open Interest: {3}, Blocks: {4}, Sweeps: {5}, Large Trades: {6}"
                .format(
                    trade_count,
                    ask_count,
                    bid_count,
                    refresh_count,
                    block_count,
                    sweep_count,
                    large_trade_count))


# Your config object MUST include the 'apiKey' and 'provider', at a minimum
config: client.Config = client.Config(
    apiKey="",
    provider=client.Providers.OPRA,
    numThreads=2,
    symbols=["AAPL"],
    # this is a static list of symbols (options contracts or option chains) that will automatically be subscribed to when the client starts
    logLevel=client.LogLevel.INFO)

# Register only the callbacks that you want.
# Take special care when registering the 'on_quote' handler as it will increase throughput by ~10x
intrinioRealtimeOptionsClient: client.Client = client.Client(config, onTrade=on_trade)

# Use this to subscribe to the entire univers of symbols (option contracts). This requires special permission.
# intrinioRealtimeOptionsClient.joinFirehose()

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
We highly encourage you to make your onTrade, onQuote, onUnusualActivity, and onOpenInterest methods has short as possible and follow a queue pattern so your app can handle the large volume of activity.
Note that quotes (ask and bid updates) comprise 99% of the volume of the entire feed. Be cautious when deciding to receive quote updates.

## Providers

Currently, Intrinio offers realtime data for this SDK from the following providers:

* OPRA - [Homepage](https://www.opraplan.com/)


## Data Format

### Trade Message

```python
class Trade:
    def __init__(self, symbol : str, price : float, size : int, totalVolume : int, timestamp : float):
        self.symbol : str = symbol
        self.price : float = price
        self.size : int = size
        self.totalVolume : int = totalVolume
        self.timestamp : float = timestamp
```

* **symbol** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **price** - the price in USD
* **size** - the size of the last trade in hundreds (each contract is for 100 shares).
* **totalVolume** - The number of contracts traded so far today.
* **timestamp** - a Unix timestamp (with microsecond precision)


### Quote Message

```python
class Quote:
    def __init__(self, symbol : str, type : QuoteType, price : float, size : int, timestamp : float):
        self.symbol : str = symbol
        self.type : QuoteType = type
        self.price : float = price
        self.size : int = size
        self.timestamp : float = timestamp
```

* **type** - the quote type
  *    **`ask`** - represents an ask type
  *    **`bid`** - represents a bid type  
* **symbol** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **price** - the price in USD
* **size** - the size of the last ask or bid in hundreds (each contract is for 100 shares).
* **timestamp** - a Unix timestamp (with microsecond precision)


### Open Interest Message

```python
class Refresh:
    def __init__(self, symbol : str, openInterest : int, timestamp : float):
        self.symbol : str = symbol
        self.openInterest : int = openInterest
        self.timestamp : float = timestamp
```

* **symbol** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **timestamp** - a Unix timestamp (with microsecond precision)
* **openInterest** - the total quantity of opened contracts as reported at the start of the trading day

### Unusual Activity Message
```python
class UnusualActivity:
    def __init__(self,
        symbol : str,
        type : UnusualActivityType,
        sentiment : UnusualActivitySentiment,
        totalValue : float,
        totalSize : int,
        averagePrice : float,
        askAtExecution : float,
        bidAtExecution : float,
        priceAtExecution : float,
        timestamp : float):
        self.symbol : str = symbol
        self.type : UnusualActivityType = type
        self.sentiment : UnusualActivitySentiment = sentiment
        self.totalValue : float = totalValue
        self.totalSize : int = totalSize
        self.averagePrice : float = averagePrice
        self.askAtExecution : float = askAtExecution
        self.bidAtExecution : float = bidAtExecution
        self.priceAtExecution : float = priceAtExecution
        self.timestamp : float = timestamp
```

* **Symbol** - Identifier for the options contract.  This includes the ticker symbol, put/call, expiry, and strike price.
* **Type** - The type of unusual activity that was detected
  *    **`Block`** - represents an 'block' trade
  *    **`Sweep`** - represents an intermarket sweep
  *    **`Large`** - represents a trade of at least $100,000
* **Sentiment** - The sentiment of the unusual activity event
  *    **`Neutral`** - Reflects a minimal expected price change
  *    **`Bullish`** - Reflects an expected positive (upward) change in price
  *    **`Bearish`** - Reflects an expected negative (downward) change in price
* **TotalValue** - The total value of the trade in USD. 'Sweeps' and 'blocks' can be comprised of multiple trades. This is the value of the entire event.
* **TotalValue** - The total size of the trade in number of contracts. 'Sweeps' and 'blocks' can be comprised of multiple trades. This is the total number of contracts exchanged during the event.
* **AveragePrice** - The average price at which the trade was executed. 'Sweeps' and 'blocks' can be comprised of multiple trades. This is the average trade price for the entire event.
* **AskAtExecution** - The 'ask' price of the underlying at execution of the trade event.
* **BidAtExecution** - The 'bid' price of the underlying at execution of the trade event.
* **PriceAtExecution** - The last trade price of the underlying at execution of the trade event.
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
It is also possible to subscribe to the entire universe of option contracts by switching the `Provider` to "OPRA_FIREHOSE" (in the config object) and calling `joinFirehose`.
The volume of data provided by the `Firehose` exceeds 100Mbps and requires special authorization.
After subscribing, using your starting list of symbols, you will call the `start` method. The client will immediately attempt to authorize your API key (provided in the config object). If authoriztion is successful, the necessary connection(s) will be opened.
If you are using the non-firehose feed, you may update your subscriptions on the fly, using the `join` and `leave` methods.
The WebSocket client is designed for near-indefinite operation. It will automatically reconnect if a connection drops/fails and when then servers turn on every morning.
If you wish to perform a graceful shutdown of the application, please call the `stop` method.

### Methods

`client : Client = Client(config : Config, onTrade : Callable[[Trade], None], onQuote : Callable[[Quote], None] = None, onOpenInterest : Callable[[Refresh], None] = None, onUnusualActivity : Callable[[UnusualActivity],None] = None)` - Creates an Intrinio Real-Time client.
* **Parameter** `config`: The configuration to be used by the client.
* **Parameter** `onTrade`: The Callable accepting trades. If no `onTrade` callback is provided, you will not receive trade updates from the server.
* **Parameter** `onQuote`: The Callable accepting quotes. If no `onQuote` callback is provided, you will not receive quote (ask, bid) updates from the server.
* **Parameter** `onOpenInterest`: The Callable accepting open interest messages. If no `onOpenInterest` callback is provided, you will not receive open interest data from the server. Note: open interest data is only updated at the beginning of every trading day. If this callback is provided you will recieve an update immediately, as well as every 15 minutes (approx).
* **Parameter** `onUnusualActivity`: The Callable accepting unusual activity events. If no `onUnusualActivity` callback is provided, you will not receive unusual activity updates from the server.

---------

`client.start()` - Starts the Intrinio Realtime WebSocket Client.
	This method will immediately attempt to authorize the API key (provided in config).
	After successful authorization, all of the data processing threads will be started, and the websocket connections will be opened.
	If a subscription has already been created with one of the `join` methods, data will begin to flow.

---------

`client.join()` - Joins channel(s) configured in config.json.
`client.join(*channels)` - Joins the provided channel or channels. E.g. "AAPL", "GOOG__210917C01040000"
`client.joinFirehose()` - Joins the firehose channel. This requires special account permissions.

---------

`client.leave()` - Leaves all joined channels/subscriptions, including firehose.
`client.leave(*channels)` - Leaves the specified channel or channels. E.g. "AAPL" or "GOOG__210917C01040000"
`client.leaveFirehose()` Leaves the firehose channel 

---------
`client.stop();` - Stops the Intrinio Realtime WebSocket Client. This method will leave all joined channels, stop all threads, and gracefully close the websocket connection(s).


## Configuration

### config.json
```python
class Config:
    def __init__(self, apiKey : str, provider : Providers, numThreads : int = 4, logLevel : LogLevel = LogLevel.INFO, manualIpAddress : str = None, symbols : set[str] = None):
        self.apiKey : str = apiKey
        self.provider : Providers = provider # Providers.OPRA or Providers.OPRA_FIREHOSE (for subscribing to all channels)
        self.numThreads : int = numThreads # At least 4 threads are recommended for 'FIREHOSE' connections
        self.manualIpAddress : str = manualIpAddress
        self.symbols : list[str] = symbols # Static list of symbols to use
        self.logLevel : LogLevel = logLevel
```