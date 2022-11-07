from distutils.command.config import config
import queue
import time
import threading
import requests
import websocket
import logging
import struct
import json
from collections.abc import Callable
from enum import IntEnum, unique

_SELF_HEAL_BACKOFFS = [10, 30, 60, 300, 600]
_HEARTBEAT_INTERVAL = 20
_EMPTY_STRING = ""
_TRADE_MESSAGE_SIZE = 72  # 61 used + 11 pad
_QUOTE_MESSAGE_SIZE = 52  # 48 used + 4 pad
_REFRESH_MESSAGE_SIZE = 52  # 44 used + 8 pad
_UNUSUAL_ACTIVITY_MESSAGE_SIZE = 74  # 62 used + 12 pad
_NAN = float("NAN")

_stopFlag: threading.Event = threading.Event()
_dataMsgLock: threading.Lock = threading.Lock()
_dataMsgCount: int = 0
_txtMsgLock: threading.Lock = threading.Lock()
_txtMsgCount: int = 0
_logHandler: logging.Logger = logging.StreamHandler()
_logHandler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
_log: logging.Logger = logging.getLogger('intrinio_realtime_options')
_log.setLevel(logging.INFO)
_log.addHandler(_logHandler)


def log(message: str):
    _log.info(message)


def do_backoff(fn: Callable[[None], bool]):
    i: int = 0
    backoff: int = _SELF_HEAL_BACKOFFS[i]
    success: bool = fn()
    while (not success):
        time.sleep(backoff)
        i = min(i + 1, len(_SELF_HEAL_BACKOFFS) - 1)
        backoff = _SELF_HEAL_BACKOFFS[i]
        success = fn()


@unique
class Providers(IntEnum):
    OPRA = 1
    MANUAL = 2


@unique
class LogLevel(IntEnum):
    DEBUG = logging.DEBUG
    INFO = logging.INFO


class Quote:
    def __init__(self, contract: str, ask_price: float, ask_size: int, bid_price: float, bid_size: int, timestamp: float):
        self.contract: str = contract
        self.ask_price: float = ask_price
        self.bid_price: float = bid_price
        self.ask_size: int = ask_size
        self.bid_size: int = bid_size
        self.timestamp: float = timestamp

    def __str__(self) -> str:
        return "Quote (Contract: {0}, AskPrice: {1:.2f}, AskSize: {2}, BidPrice: {3:.2f}, BidSize: {4}, Timestamp: {5})"\
               .format(self.contract,
                       self.ask_price,
                       self.ask_size,
                       self.bid_price,
                       self.bid_size,
                       self.timestamp)

    def get_strike_price(self) -> float:
        return float(self.contract[(self.contract.index('_') + 8):])

    def is_put(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'P'

    def is_call(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'C'

    def get_expiration_date(self) -> time.struct_time:
        date_start_index: int = self.contract.index('_') + 1
        return time.strptime(self.contract[date_start_index: (date_start_index+6)], "%y%m%d")

    def get_underlying_symbol(self) -> str:
        return self.contract[0:self.contract.index('_')].trim()


class Trade:
    def __init__(self, contract: str, price: float, size: int, timestamp: float, total_volume: int, ask_price_at_execution: float, bid_price_at_execution: float, underlying_price_at_execution: float):
        self.contract: str = contract
        self.price: float = price
        self.size: int = size
        self.timestamp: float = timestamp
        self.total_volume: int = total_volume
        self.ask_price_at_execution = ask_price_at_execution
        self.bid_price_at_execution = bid_price_at_execution
        self.underlying_price_at_execution = underlying_price_at_execution

    def __str__(self) -> str:
        return "Trade (Contract: {0}, Price: {1:.2f}, Size: {2}, Timestamp: {3}, TotalVolume: {4}, AskPriceAtExecution: {5:.2f}, BidPriceAtExecution: {6:.2f}, UnderlyingPriceAtExecution: {7:.2f})"\
               .format(self.contract,
                       self.price,
                       self.size,
                       self.timestamp,
                       self.total_volume,
                       self.ask_price_at_execution,
                       self.bid_price_at_execution,
                       self.underlying_price_at_execution)

    def get_strike_price(self) -> float:
        return float(self.contract[(self.contract.index('_') + 8):])

    def is_put(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'P'

    def is_call(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'C'

    def get_expiration_date(self) -> time.struct_time:
        date_start_index: int = self.contract.index('_') + 1
        return time.strptime(self.contract[date_start_index: (date_start_index + 6)], "%y%m%d")

    def get_underlying_symbol(self) -> str:
        return self.contract[0:self.contract.index('_')].trim()


@unique
class UnusualActivitySentiment(IntEnum):
    NEUTRAL = 0
    BULLISH = 1
    BEARISH = 2


@unique
class UnusualActivityType(IntEnum):
    BLOCK = 3
    SWEEP = 4
    LARGE = 5
    GOLDEN_EGG = 6


class Refresh:
    def __init__(self, contract: str, open_interest: int, open_price: float, close_price: float, high_price: float, low_price: float):
        self.contract: str = contract
        self.open_interest: int = open_interest
        self.open_price: float = open_price
        self.close_price: float = close_price
        self.high_price: float = high_price
        self.low_price: float = low_price

    def __str__(self) -> str:
        return "Refresh (Contract: {0}, OpenInterest: {1}, OpenPrice: {2:.2f}, ClosePrice: {3:.2f}, HighPrice: {4:.2f}, LowPrice: {5:.2f})"\
               .format(self.contract,
                       self.open_interest,
                       self.open_price,
                       self.close_price,
                       self.high_price,
                       self.low_price)

    def get_strike_price(self) -> float:
        return float(self.contract[(self.contract.index('_') + 8):])

    def is_put(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'P'

    def is_call(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'C'

    def get_expiration_date(self) -> time.struct_time:
        date_start_index: int = self.contract.index('_') + 1
        return time.strptime(self.contract[date_start_index: (date_start_index+6)], "%y%m%d")

    def get_underlying_symbol(self) -> str:
        return self.contract[0:self.contract.index('_')].trim()


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

    def __str__(self) -> str:
        return "Unusual Activity (Contract: {0}, Type: {1}, Sentiment: {2}, Total Value: {3:.2f}, Total Size: {4}, Average Price: {5:.2f}, Ask at Execution: {6:.2f}, Bid at Execution: {7:.2f}, Underlying Price at Execution: {8:.2f}, Timestamp: {9})"\
                .format(self.contract,
                        self.activity_type,
                        self.sentiment,
                        self.total_value,
                        self.total_size,
                        self.average_price,
                        self.ask_price_at_execution,
                        self.bid_price_at_execution,
                        self.underlying_price_at_execution,
                        self.timestamp)

    def get_strike_price(self) -> float:
        return float(self.contract[(self.contract.index('_') + 8):])

    def is_put(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'P'

    def is_call(self) -> bool:
        return self.contract[(self.contract.index('_') + 7)] == 'C'

    def get_expiration_date(self) -> time.struct_time:
        date_start_index: int = self.contract.index('_') + 1
        return time.strptime(self.contract[date_start_index: (date_start_index + 6)], "%y%m%d")

    def get_underlying_symbol(self) -> str:
        return self.contract[0:self.contract.index('_')].trim()


def _get_option_mask(use_on_trade: bool, use_on_quote: bool, use_on_refresh: bool, use_on_unusual_activity: bool) -> int:
    mask: int = 0
    if use_on_trade:
        mask |= 0b0001
    if use_on_quote:
        mask |= 0b0010
    if use_on_refresh:
        mask |= 0b0100
    if use_on_unusual_activity:
        mask |= 0b1000
    return mask


class _WebSocket(websocket.WebSocketApp):
    def __init__(self,
                 ws_url: str,
                 ws_lock: threading.Lock,
                 heartbeat_thread: threading.Thread,
                 worker_threads: list[threading.Thread],
                 get_channels: Callable[[None], set[tuple[str, bool]]],
                 get_token: Callable[[None], str],
                 get_url: Callable[[str], str],
                 use_on_trade: bool,
                 use_on_quote: bool,
                 use_on_refresh: bool,
                 use_on_ua: bool,
                 data_queue: queue.Queue):
        super().__init__(ws_url, on_open=self.__on_open, on_close=self.__on_close, on_data=self.__on_data, on_error=self.__on_error)
        self.__wsLock: threading.Lock = ws_lock
        self.__heartbeat_thread: threading.Thread = heartbeat_thread
        self.__worker_threads: list[threading.Thread] = worker_threads
        self.__get_channels: Callable[[None], set[tuple[str, bool]]] = get_channels
        self.__get_token: Callable[[None], str] = get_token
        self.__get_url: Callable[[str], str] = get_url
        self.__use_on_trade: bool = use_on_trade
        self.__use_on_quote: bool = use_on_quote
        self.__use_on_refresh: bool = use_on_refresh
        self.__use_on_ua: bool = use_on_ua
        self.__data_queue: queue.Queue = data_queue
        self.__is_reconnecting: bool = False
        self.__last_reset: float = time.time()
        self.isReady: bool = False

    def __on_open(self, ws):
        _log.info("Websocket - Connected")
        self.__wsLock.acquire()
        try:
            self.isReady = True
            self.__is_reconnecting = False
            if not self.__heartbeat_thread.is_alive():
                self.__heartbeat_thread.start()
            for worker in self.__worker_threads:
                if not worker.is_alive():
                    worker.start()
        finally:
            self.__wsLock.release()
        if self.__get_channels and callable(self.__get_channels):
            channels: set[str] = self.__get_channels()
            if channels and (len(channels) > 0):
                for symbol in channels:
                    symbol_bytes = bytes(symbol, 'utf-8')
                    message: bytes = bytearray(len(symbol_bytes) + 2)
                    message[0] = 74  # join code
                    message[1] = _get_option_mask(self.__use_on_trade, self.__use_on_quote, self.__use_on_refresh, self.__use_on_ua)
                    message[2:] = symbol_bytes
                    if self.isReady:
                        _log.info("Websocket - Joining channel: {0}".format(symbol))
                        self.send_binary(message)

    def __try_reconnect(self) -> bool:
        _log.info("Websocket - Reconnecting...")
        if self.isReady:
            return True
        else:
            with self.__wsLock:
                self.__is_reconnecting = True
            if (time.time() - self.__last_reset > (60 * 60 * 24 * 5)):
                token: str = self.__get_token()
                super().url = self.__get_url(token)
            self.start()
            return False

    def __on_close(self, ws, closeStatusCode, closeMsg):
        self.__wsLock.acquire()
        try:
            if (not self.__is_reconnecting):
                _log.info("Websocket - Closed - {0}: {1}".format(closeStatusCode, closeMsg))
                self.isReady = False
                if (not _stopFlag.is_set()):
                    do_backoff(self.__try_reconnect)
        finally:
            self.__wsLock.release()

    def __on_error(self, ws, error):
        _log.error("Websocket - Error - {0}".format(error))

    def __on_data(self, ws, data, code, continueFlag):
        if code == websocket.ABNF.OPCODE_BINARY:
            with _dataMsgLock:
                global _dataMsgCount
                _dataMsgCount += 1
            self.__data_queue.put_nowait(data)
        else:
            _log.debug("Websocket - Message received")
            with _txtMsgLock:
                global _txtMsgCount
                _txtMsgCount += 1
            if data == _EMPTY_STRING:
                _log.debug("Heartbeat response received")
            else:
                _log.error("Error received: {0}".format(data))

    def start(self):
        super().run_forever(skip_utf8_validation=True)
        # super().run_forever(ping_interval = 5, ping_timeout = 2, skip_utf8_validation = True)

    def stop(self):
        super().close()

    def send_heartbeat(self):
        if self.isReady:
            super().send(_EMPTY_STRING, websocket.ABNF.OPCODE_TEXT)

    def send(self, message: str):
        super().send(message, websocket.ABNF.OPCODE_TEXT)

    def send_binary(self, message: bytes):
        super().send(message, websocket.ABNF.OPCODE_BINARY)

    def reset(self):
        self.__last_reset = time.time()


class Config:
    def __init__(self, apiKey: str, provider: Providers, numThreads: int = 4, logLevel: LogLevel = LogLevel.INFO,
                 manualIpAddress: str = None, symbols: set[str] = None):
        self.apiKey: str = apiKey
        self.provider: Providers = provider
        self.numThreads: int = numThreads
        self.manualIpAddress: str = manualIpAddress
        self.symbols: list[str] = symbols
        self.logLevel: LogLevel = logLevel


def _heartbeat_fn(ws_lock: threading.Lock, web_socket: _WebSocket):
    _log.debug("Starting heartbeat")
    while not _stopFlag.is_set():
        time.sleep(_HEARTBEAT_INTERVAL)
        _log.debug("Sending heartbeat")
        ws_lock.acquire()
        try:
            if web_socket and not _stopFlag.is_set():
                web_socket.send_heartbeat()
        finally:
            ws_lock.release()
    _log.debug("Heartbeat stopped")


def _get_seconds_from_epoch_from_ticks(ticks: int) -> float:
    return float(ticks) / 1_000_000_000.0


def _scale_value(value: int, scale_type: int) -> float:
    # if scale_type == 0x00:
    #     return float(value)  # divided by 1
    # elif scale_type == 0x01:
    #     return float(value) / 10.0
    # elif scale_type == 0x02:
    #     return float(value) / 100.0
    # elif scale_type == 0x03:
    #     return float(value) / 1_000.0
    # elif scale_type == 0x04:
    #     return float(value) / 10_000.0
    # elif scale_type == 0x05:
    #     return float(value) / 100_000.0
    # elif scale_type == 0x06:
    #     return float(value) / 1_000_000.0
    # elif scale_type == 0x07:
    #     return float(value) / 10_000_000.0
    # elif scale_type == 0x08:
    #     return float(value) / 100_000_000.0
    # elif scale_type == 0x09:
    #     return float(value) / 1_000_000_000.0
    # elif scale_type == 0x0A:
    #     return float(value) / 512.0
    # elif scale_type == 0x0F:
    #     return 0.0
    # else:
    #     return float(value)  # divided by 1
    match scale_type:
        case 0x00:
            return float(value)  # divided by 1
        case 0x01:
            return float(value) / 10.0
        case 0x02:
            return float(value) / 100.0
        case 0x03:
            return float(value) / 1_000.0
        case 0x04:
            return float(value) / 10_000.0
        case 0x05:
            return float(value) / 100_000.0
        case 0x06:
            return float(value) / 1_000_000.0
        case 0x07:
            return float(value) / 10_000_000.0
        case 0x08:
            return float(value) / 100_000_000.0
        case 0x09:
            return float(value) / 1_000_000_000.0
        case 0x0A:
            return float(value) / 512.0
        case 0x0F:
            return 0.0
        case _:
            return float(value)  # divided by 1


def _scale_uint64(value: int, scale_type: int) -> float:
    if value == 18446744073709551615:
        return _NAN
    else:
        return _scale_value(value, scale_type)


def _scale_int32(value: int, scale_type: int) -> float:
    if value == 2147483647 or value == -2147483648:
        return _NAN
    else:
        return _scale_value(value, scale_type)


def _thread_fn(index: int, data: queue.Queue,
               on_trade: Callable[[Trade], None],
               on_quote: Callable[[Quote], None] = None,
               on_refresh: Callable[[Refresh], None] = None,
               on_unusual_activity: Callable[[UnusualActivity], None] = None):
    _log.debug("Starting worker thread {0}".format(index))
    while not _stopFlag.is_set():
        try:
            datum: bytes = data.get(True, 1.0)
            count: int = datum[0]
            start_index: int = 1
            for _ in range(count):
                msg_type: int = datum[start_index + 22]
                if msg_type == 1:  # Quote
                    message: bytes = datum[start_index:(start_index + _QUOTE_MESSAGE_SIZE)]
                    # byte structure:
                    # 	contract length [0]
                    # 	contract [1-21] utf-8 string
                    # 	event type [22] uint8
                    # 	price type [23] uint8
                    # 	ask price [24-27] int32
                    # 	ask size [28-31] uint32
                    # 	bid price [32-35] int32
                    # 	bid size [36-39] uint32
                    # 	timestamp [40-47] uint64
                    # https://docs.python.org/3/library/struct.html#format-characters
                    contract: str = message[1:message[0]].decode('ascii')
                    ask_price: float = _scale_int32(struct.unpack_from('<l', message, 24)[0], message[23])
                    ask_size: int = struct.unpack_from('<L', message, 28)[0]
                    bid_price: float = _scale_int32(struct.unpack_from('<l', message, 32)[0], message[23])
                    bid_size: int = struct.unpack_from('<L', message, 36)[0]
                    timestamp: float = _get_seconds_from_epoch_from_ticks(struct.unpack_from('<Q', message, 40)[0])
                    if on_quote:
                        on_quote(Quote(contract, ask_price, ask_size, bid_price, bid_size, timestamp))
                    start_index = start_index + _QUOTE_MESSAGE_SIZE
                elif msg_type == 0:  # Trade
                    message: bytes = datum[start_index:(start_index + _TRADE_MESSAGE_SIZE)]
                    #  byte structure:
                    #  contract length [0] uint8
                    #  contract [1-21] utf-8 string
                    #  event type [22] uint8
                    #  price type [23] uint8
                    #  underlying price type [24] uint8
                    #  price [25-28] int32
                    #  size [29-32] uint32
                    #  timestamp [33-40] uint64
                    #  total volume [41-48] uint64
                    #  ask price at execution [49-52] int32
                    #  bid price at execution [53-56] int32
                    #  underlying price at execution [57-60] int32
                    # https://docs.python.org/3/library/struct.html#format-characters
                    contract: str = message[1:message[0]].decode('ascii')
                    price: float = _scale_int32(struct.unpack_from('<l', message, 25)[0], message[23])
                    size: int = struct.unpack_from('<L', message, 29)[0]
                    timestamp: float = _get_seconds_from_epoch_from_ticks(struct.unpack_from('<Q', message, 33)[0])
                    total_volume: int = struct.unpack_from('<Q', message, 41)[0]
                    ask_price_at_execution: int = _scale_int32(struct.unpack_from('<l', message, 49)[0], message[23])
                    bid_price_at_execution: int = _scale_int32(struct.unpack_from('<l', message, 53)[0], message[23])
                    underlying_price_at_execution: int = _scale_int32(struct.unpack_from('<l', message, 57)[0], message[24])
                    if on_trade:
                        on_trade(Trade(contract, price, size, timestamp, total_volume, ask_price_at_execution, bid_price_at_execution, underlying_price_at_execution))
                    start_index = start_index + _TRADE_MESSAGE_SIZE
                elif msg_type > 2:  # Unusual Activity
                    message: bytes = datum[start_index:(start_index + _UNUSUAL_ACTIVITY_MESSAGE_SIZE)]
                    # byte structure:
                    # contract length [0] uint8
                    # contract [1-21] utf-8 string
                    # event type [22] uint8
                    # sentiment type [23] uint8
                    # price type [24] uint8
                    # underlying price type [25] uint8
                    # total value [26-33] uint64
                    # total size [34-37] uint32
                    # average price [38-41] int32
                    # ask price at execution [42-45] int32
                    # bid price at execution [46-49] int32
                    # underlying price at execution [50-53] int32
                    # timestamp [54-61] uint64
                    # https://docs.python.org/3/library/struct.html#format-characters
                    contract: str = message[1:message[0]].decode('ascii')
                    activity_type: UnusualActivityType = message[22]
                    sentiment: UnusualActivitySentiment = message[23]
                    total_value: float = _scale_uint64(struct.unpack_from('<Q', message, 26)[0], message[24])
                    total_size: int = struct.unpack_from('<L', message, 34)[0]
                    average_price: float = _scale_int32(struct.unpack_from('<l', message, 38)[0], message[24])
                    ask_price_at_execution: float = _scale_int32(struct.unpack_from('<l', message, 42)[0], message[24])
                    bid_price_at_execution: float = _scale_int32(struct.unpack_from('<l', message, 46)[0], message[24])
                    underlying_price_at_execution: float = _scale_int32(struct.unpack_from('<l', message, 50)[0], message[25])
                    timestamp: float = _get_seconds_from_epoch_from_ticks(struct.unpack_from('<Q', message, 54)[0])
                    if on_unusual_activity:
                        on_unusual_activity(UnusualActivity(contract, activity_type, sentiment, total_value, total_size, average_price, ask_price_at_execution, bid_price_at_execution, underlying_price_at_execution, timestamp))
                    start_index = start_index + _UNUSUAL_ACTIVITY_MESSAGE_SIZE
                elif msg_type == 2:  # Refresh
                    message: bytes = datum[start_index:(start_index + _REFRESH_MESSAGE_SIZE)]
                    # byte structure:
                    # contract length [0] uint8
                    # contract [1-21] utf-8 string
                    # event type [22] uint8
                    # price type [23] uint8
                    # open interest [24-27] uint32
                    # open price [28-31] int32
                    # close price [32-35] int32
                    # high price [36-39] int32
                    # low price [40-43] int32
                    contract: str = message[1:message[0]].decode('ascii')
                    open_interest: int = struct.unpack_from('<L', message, 24)[0]
                    open_price: float = _scale_int32(struct.unpack_from('<l', message, 28)[0], message[23])
                    close_price: float = _scale_int32(struct.unpack_from('<l', message, 32)[0], message[23])
                    high_price: float = _scale_int32(struct.unpack_from('<l', message, 36)[0], message[23])
                    low_price: float = _scale_int32(struct.unpack_from('<l', message, 40)[0], message[23])
                    if on_refresh:
                        on_refresh(Refresh(contract, open_interest, open_price, close_price, high_price, low_price))
                    start_index = start_index + _REFRESH_MESSAGE_SIZE
                else:
                    _log.warn("Invalid Message Type: {0}".format(msg_type))
        except queue.Empty:
            continue
    _log.debug("Worker thread {0} stopped".format(index))


class Client:
    def __init__(self, config: Config, on_trade: Callable[[Trade], None], on_quote: Callable[[Quote], None] = None,
                 on_refresh: Callable[[Refresh], None] = None,
                 on_unusual_activity: Callable[[UnusualActivity], None] = None):
        if not config:
            raise ValueError("Config is required")
        if (not config.apiKey) or (not isinstance(config.apiKey, str)):
            raise ValueError("You must provide a valid API key")
        if (not config.provider) or (not isinstance(config.provider, Providers)):
            raise ValueError("You must specify a valid provider")
        if ((config.provider == Providers.MANUAL) or (config.provider == Providers.MANUAL_FIREHOSE)) and (
                (not config.manualIpAddress) or (not isinstance(config.manualIpAddress, str))):
            raise ValueError("You must specify an IP address for a manual configuration")
        if on_trade:
            if callable(on_trade):
                self.__use_on_trade: bool = True
            else:
                raise ValueError("Parameter 'on_trade' must be a function")
        else:
            self.__use_on_trade: bool = False
        if on_quote:
            if callable(on_quote):
                self.__use_on_quote: bool = True
            else:
                raise ValueError("Parameter 'on_quote' must be a function")
        else:
            self.__use_on_quote: bool = False
        if on_refresh:
            if callable(on_refresh):
                self.__use_on_refresh: bool = True
            else:
                raise ValueError("Parameter 'on_refresh' must be a function")
        else:
            self.__use_on_refresh: bool = False
        if on_unusual_activity:
            if callable(on_unusual_activity):
                self.__use_on_unusual_activity: bool = True
            else:
                raise ValueError("Parameter 'on_unusual_activity' must be a function")
        else:
            self.__use_on_unusual_activity: bool = False
        self.__provider: Providers = config.provider
        self.__apiKey: str = config.apiKey
        self.__manualIP: str = config.manualIpAddress
        self.__token: tuple[str, float] = (None, 0.0)
        self.__webSocket: _WebSocket = None
        if config.symbols and (isinstance(config.symbols, list)) and (len(config.symbols) > 0):
            self.__channels: set[str] = set((symbol) for symbol in config.symbols)
        else:
            self.__channels: set[str] = set()
        self.__data: queue.Queue = queue.Queue()
        self.__t_lock: threading.Lock = threading.Lock()
        self.__ws_lock: threading.Lock = threading.Lock()
        self.__heartbeat_thread: threading.Thread = threading.Thread(None, _heartbeat_fn,
                                                                     args=[self.__ws_lock, self.__webSocket],
                                                                     daemon=True)
        self.__worker_threads: list[threading.Thread] = [threading.Thread(None,
                                                                          _thread_fn,
                                                                          args=[i, self.__data, on_trade, on_quote, on_refresh, on_unusual_activity],
                                                                          daemon=True) for i in range(config.numThreads)]
        self.__socket_thread: threading.Thread = None
        self.__is_started: bool = False
        _log.setLevel(config.logLevel)

    def __all_ready(self) -> bool:
        self.__ws_lock.acquire()
        ready: bool = True
        try:
            ready = self.__webSocket.isReady
        finally:
            self.__ws_lock.release()
        return ready

    def __get_auth_url(self) -> str:
        if self.__provider == Providers.OPRA:
            return "https://realtime-options.intrinio.com/auth?api_key=" + self.__apiKey
        elif self.__provider == Providers.MANUAL:
            return "http://" + self.__manualIP + "/auth?api_key=" + self.__apiKey
        else:
            raise ValueError("Provider not specified")

    def __get_web_socket_url(self, token: str) -> str:
        if self.__provider == Providers.OPRA:
            return "wss://realtime-options.intrinio.com/socket/websocket?vsn=1.0.0&token=" + token
        elif self.__provider == Providers.MANUAL:
            return "ws://" + self.__manualIP + "/socket/websocket?vsn=1.0.0&token=" + token
        else:
            raise ValueError("Provider not specified")

    def __try_set_token(self) -> bool:
        _log.info("Authorizing...")
        headers = {"Client-Information": "IntrinioOptionsPythonSDKv2.0"}
        try:
            response: requests.Response = requests.get(self.__get_auth_url(), headers=headers, timeout=1)
            if response.status_code != 200:
                _log.error(
                    "Authorization Failure (status code = {0}): The authorization key you provided is likely incorrect.".format(
                        response.status_code))
                return False
            self.__token = (response.text, time.time())
            _log.info("Authorization successful.")
            return True
        except requests.exceptions.Timeout:
            _log.error("Authorization Failure: The request timed out.")
            return False
        except requests.exceptions.ConnectionError as err:
            _log.error("Authorization Failure: {0}".format(err))
            return False

    def __get_token(self) -> str:
        self.__t_lock.acquire()
        try:
            if ((time.time() - self.__token[1]) > (60 * 60 * 24)):  # 60sec/min * 60min/hr * 24hrs = 1 day
                do_backoff(self.__try_set_token)
            return self.__token[0]
        finally:
            self.__t_lock.release()

    def __get_channels(self) -> set[str]:
        return self.__channels

    def __join(self, symbol: str):
        if symbol not in self.__channels:
            self.__channels.add(symbol)
            symbol_bytes = bytes(symbol, 'utf-8')
            message: bytes = bytearray(len(symbol_bytes)+2)
            message[0] = 74  # join code
            message[1] = _get_option_mask(self.__use_on_trade, self.__use_on_quote, self.__use_on_refresh, self.__use_on_unusual_activity)
            message[2:] = symbol_bytes
            if self.__webSocket.isReady:
                _log.info("Websocket - Joining channel: {0}".format(symbol))
                self.__webSocket.send_binary(message)

    def __leave(self, symbol: str):
        if symbol in self.__channels:
            self.__channels.remove(symbol)
            symbol_bytes = bytes(symbol, 'utf-8')
            message: bytes = bytearray(len(symbol_bytes) + 2)
            message[0] = 76  # leave code
            message[1] = _get_option_mask(self.__use_on_trade, self.__use_on_quote, self.__use_on_refresh, self.__use_on_unusual_activity)
            message[2:] = symbol_bytes
            if self.__webSocket.isReady:
                _log.info("Websocket - Leaving channel: {0}".format(symbol))
                self.__webSocket.send_binary(message)

    def join(self, *symbols):
        if self.__is_started:
            while not self.__all_ready():
                time.sleep(1.0)
        for (symbol) in symbols:
            if symbol not in self.__channels:
                self.__join(symbol)

    def join_firehose(self):
        if "$FIREHOSE" in self.__channels:
            _log.warn("This client has already joined the firehose channel")
        else:
            if self.__is_started:
                while not self.__all_ready():
                    time.sleep(1.0)
            self.__join("$FIREHOSE")

    def leave(self, *symbols):
        if not symbols:
            _log.info("Leaving all channels")
            channels: set[str] = self.__channels.copy()
            for (symbol) in channels:
                self.__leave(symbol)
        symbol_set: set[str] = set(symbols)
        for sym in symbol_set:
            self.__leave(sym)

    def leave_firehose(self):
        if "$FIREHOSE" in self.__channels:
            self.__leave("$FIREHOSE")

    def __socket_start_fn(self, token: str):
        _log.info("Websocket - Connecting...")
        ws_url: str = self.__get_web_socket_url(token)
        self.__webSocket = _WebSocket(ws_url,
                                      self.__ws_lock,
                                      self.__heartbeat_thread,
                                      self.__worker_threads,
                                      self.__get_channels,
                                      self.__get_token,
                                      self.__get_web_socket_url,
                                      self.__use_on_trade,
                                      self.__use_on_quote,
                                      self.__use_on_refresh,
                                      self.__use_on_unusual_activity,
                                      self.__data)
        self.__webSocket.start()

    def start(self):
        if (not (self.__use_on_trade or self.__use_on_quote or self.__use_on_refresh or self.__use_on_unusual_activity)):
            raise ValueError("You must set at least one callback method before starting client")
        token: str = self.__get_token()
        self.__ws_lock.acquire()
        try:
            self.__socket_thread = threading.Thread = threading.Thread(None, self.__socket_start_fn, args=[token], daemon=True)
        finally:
            self.__ws_lock.release()
        self.__socket_thread.start()
        self.__is_started = True

    def stop(self):
        _log.info("Stopping...")
        if len(self.__channels) > 0:
            self.leave()
        time.sleep(1.0)
        self.__ws_lock.acquire()
        try:
            self.__webSocket.isReady = False
        finally:
            self.__ws_lock.release()
        _stopFlag.set()
        self.__webSocket.stop()
        for i in range(len(self.__worker_threads)):
            self.__worker_threads[i].join()
            _log.debug("Worker thread {0} joined".format(i))
        self.__socket_thread.join()
        _log.debug("Socket thread joined")
        _log.info("Stopped")

    def get_stats(self) -> tuple[int, int, int]:
        return _dataMsgCount, _txtMsgCount, self.__data.qsize()
