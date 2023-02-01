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
    symbols=["AAPL", "BRKB__230217C00300000"],
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
intrinioRealtimeOptionsClient.join()

time.sleep(60 * 60)
# sigint, or ctrl+c, during the thread wait will also perform the same below code.
on_kill_process(None, None)
