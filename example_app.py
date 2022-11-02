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
open_interest_count = 0
open_interest_count_lock = Lock()
block_count = 0
block_count_lock = Lock()
sweep_count = 0
sweep_count_lock = Lock()
large_trade_count = 0
large_trade_count_lock = Lock()
golden_trade_count = 0
golden_trade_count_lock = Lock()


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
        client.Log("on_quote - Unknown quote type {0}", quote.type)


def on_trade(trade: client.Trade):
    global trade_count
    global trade_count_lock
    with trade_count_lock: trade_count += 1


def on_open_interest(oi: client.OpenInterest):
    global open_interest_count
    global open_interest_count_lock
    with open_interest_count_lock: open_interest_count += 1


def on_unusual_activity(ua: client.UnusualActivity):
    global block_count
    global block_count_lock
    global sweep_count
    global sweep_count_lock
    global large_trade_count
    global large_trade_count_lock
    if (ua.type == client.UnusualActivityType.BLOCK):
        with block_count_lock:
            block_count += 1
    elif (ua.type == client.UnusualActivityType.SWEEP):
        with sweep_count_lock:
            sweep_count += 1
    elif (ua.type == client.UnusualActivityType.LARGE):
        with large_trade_count_lock:
            large_trade_count += 1
    else:
        client.Log("on_unusual_activity - Unknown type {0}", ua.type)


class Summarize(threading.Thread):
    def __init__(self, stop_flag: threading.Event, client: client.Client):
        threading.Thread.__init__(self, args=(), kwargs=None, daemon=True)
        self.__stop_flag: threading.Event = stop_flag
        self.__client = client

    def run(self):
        while (not self.__stop_flag.is_set()):
            time.sleep(10.0)
            (dataMsgs, txtMsgs, queueDepth) = self.__client.getStats()
            client.Log(
                "Client Stats - Data Messages: {0}, Text Messages: {1}, Queue Depth: {2}".format(dataMsgs, txtMsgs,
                                                                                                 queueDepth))
            client.Log(
                "App Stats - Trades: {0}, Asks: {1}, Bids: {2}, Open Interest: {3}, Blocks: {4}, Sweeps: {5}, Large Trades: {6}"
                .format(
                    trade_count,
                    ask_count,
                    bid_count,
                    open_interest_count,
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
# Take special care when registering the 'onQuote' handler as it will increase throughput by ~10x
intrinioRealtimeOptionsClient: client.Client = client.Client(config, onTrade=on_trade)

# Use this to subscribe to the entire univers of symbols (option contracts). This requires special permission.
# intrinioRealtimeOptionsClient.joinFirehose()

# Use this to subscribe, dynamically, to an option chain (all option contracts for a given underlying symbol).
# intrinioRealtimeOptionsClient.join("AAPL")

# Use this to subscribe, dynamically, to a specific option contract.
# intrinioRealtimeOptionsClient.join("AAP___230616P00250000")

# Use this to subscribe, dynamically, a list of specific option contracts or option chains.
# intrinioRealtimeOptionsClient.join("GOOG__220408C02870000", "MSFT__220408C00315000", "AAPL__220414C00180000", "TSLA", "GE")

stop_event = Event()


def on_kill_process(sig, frame):
    client.Log("Sample Application - Stopping")
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
