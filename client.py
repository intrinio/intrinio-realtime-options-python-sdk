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
_HEARTBEAT_MESSAGE = ""
_ERROR_RESPONSE = "\"status\":\"error\""
_TRADE_MESSAGE_SIZE = 72  # 61 used + 11 pad
_QUOTE_MESSAGE_SIZE = 52  # 48 used + 4 pad
_REFRESH_MESSAGE_SIZE = 52  # 44 used + 8 pad
_UNUSUAL_ACTIVITY_MESSAGE_SIZE = 74  # 62 used + 12 pad

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
    GOLDEN = 6


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


class _WebSocket(websocket.WebSocketApp):
    def __init__(self,
                 wsUrl: str,
                 wsLock: threading.Lock,
                 hearbeatThread: threading.Thread,
                 workerThreads: list[threading.Thread],
                 getChannels: Callable[[None], set[tuple[str, bool]]],
                 getToken: Callable[[None], str],
                 getUrl: Callable[[str], str],
                 useOnTrade: bool,
                 useOnQuote: bool,
                 useOnOI: bool,
                 useOnUA: bool,
                 dataQueue: queue.Queue):
        super().__init__(wsUrl, on_open=self.__onOpen, on_close=self.__onClose, on_data=self.__onData, on_error=self.__onError)
        self.__wsLock: threading.Lock = wsLock
        self.__heartbeatThread: threading.Thread = hearbeatThread
        self.__workerThreads: list[threading.Thread] = workerThreads
        self.__getChannels: Callable[[None], set[tuple[str, bool]]] = getChannels
        self.__getToken: Callable[[None], str] = getToken
        self.__getUrl: Callable[[str], str] = getUrl
        self.__useOnTrade: bool = useOnTrade
        self.__useOnQuote: bool = useOnQuote
        self.__useOnOI: bool = useOnOI
        self.__useOnUA: bool = useOnUA
        self.__dataQueue: queue.Queue = dataQueue
        self.__isReconnecting: bool = False
        self.__lastReset: float = time.time()
        self.isReady: bool = False

    def __onOpen(self, ws):
        _log.info("Websocket - Connected")
        self.__wsLock.acquire()
        try:
            self.isReady = True
            self.__isReconnecting = False
            if not self.__heartbeatThread.is_alive():
                self.__heartbeatThread.start()
            for worker in self.__workerThreads:
                if (not worker.is_alive()):
                    worker.start()
        finally:
            self.__wsLock.release()
        if self.__getChannels and callable(self.__getChannels):
            channels: set[str] = self.__getChannels()
            if channels and (len(channels) > 0):
                for symbol in channels:
                    subscriptionSelection: str = ""
                    subscriptionList: list[str] = list()
                    if (self.__useOnTrade):
                        subscriptionSelection += ",\"trade_data\":\"true\""
                        subscriptionList.append("trade")
                    if (self.__useOnQuote):
                        subscriptionSelection += ",\"quote_data\":\"true\""
                        subscriptionList.append("quote")
                    if (self.__useOnOI):
                        subscriptionSelection += ",\"open_interest_data\":\"true\""
                        subscriptionList.append("open interest")
                    if (self.__useOnUA):
                        subscriptionSelection += ",\"unusual_activity_data\":\"true\""
                        subscriptionList.append("unusual activity")
                    message: str = "{\"topic\":\"options:" + symbol + "\",\"event\":\"phx_join\"" + subscriptionSelection + ",\"payload\":{},\"ref\":null}"
                    subscriptionSelection = ", ".join(subscriptionList)
                    _log.info("Websocket - Joining channel: {0} (subscriptions = {1})".format(symbol, subscriptionSelection))
                    super().send(message, websocket.ABNF.OPCODE_TEXT)

    def __tryReconnect(self) -> bool:
        _log.info("Websocket - Reconnecting...")
        if self.isReady:
            return True
        else:
            with self.__wsLock:
                self.__isReconnecting = True
            if (time.time() - self.__lastReset > (60 * 60 * 24 * 5)):
                token: str = self.__getToken()
                super().url = self.__getUrl(token)
            self.start()
            return False

    def __onClose(self, ws, closeStatusCode, closeMsg):
        self.__wsLock.acquire()
        try:
            if (not self.__isReconnecting):
                _log.info("Websocket - Closed - {0}: {1}".format(closeStatusCode, closeMsg))
                self.isReady = False
                if (not _stopFlag.is_set()):
                    do_backoff(self.__tryReconnect)
        finally:
            self.__wsLock.release()

    def __onError(self, ws, error):
        _log.error("Websocket - Error - {0}".format(error))

    def __onData(self, ws, data, type, continueFlag):
        if (type == websocket.ABNF.OPCODE_BINARY):
            with _dataMsgLock:
                global _dataMsgCount
                _dataMsgCount += 1
            self.__dataQueue.put_nowait(data)
        else:
            _log.debug("Websocket - Message received")
            with _txtMsgLock:
                global _txtMsgCount
                _txtMsgCount += 1
            if (data == _HEARTBEAT_RESPONSE):
                _log.debug("Heartbeat resonse received")
            elif (_ERROR_RESPONSE in data):
                jsonDoc = json.loads(data)
                errorMsg = jsonDoc["payload"]["response"]
                _log.error("Error received: {0}".format(errorMsg))

    def start(self):
        super().run_forever(skip_utf8_validation=True)
        # super().run_forever(ping_interval = 5, ping_timeout = 2, skip_utf8_validation = True)

    def stop(self):
        super().close()

    def sendHeartbeat(self):
        if self.isReady:
            super().send(_HEARTBEAT_MESSAGE, websocket.ABNF.OPCODE_TEXT)

    def send(self, message: str):
        super().send(message, websocket.ABNF.OPCODE_TEXT)

    def send_binary(self, message: bytes):
        super().send(message, websocket.ABNF.OPCODE_BINARY)

    def reset(self):
        self.lastReset = time.time()


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
            if not _stopFlag.is_set():
                web_socket.sendHeartbeat()
        finally:
            ws_lock.release()
    _log.debug("Heartbeat stopped")


def _get_seconds_from_epoch_from_ticks(ticks: int) -> float:
    return float(ticks) / 1_000_000_000.0


def _scale_number(value: int, scale_type: int) -> float:
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
                    ask_price: float = _scale_number(struct.unpack_from('<i', message, 24)[0], message[23])
                    ask_size: int = struct.unpack_from('<I', message, 28)[0]
                    bid_price: float = _scale_number(struct.unpack_from('<i', message, 32)[0], message[23])
                    bid_size: int = struct.unpack_from('<I', message, 36)[0]
                    timestamp: float = _get_seconds_from_epoch_from_ticks(struct.unpack_from('<L', message, 34)[0])
                    if on_quote:
                        on_quote(Quote(contract, ask_price, ask_size, bid_price, bid_size, timestamp))
                    start_index = start_index + _QUOTE_MESSAGE_SIZE
                elif msg_type == 0:  # Trade
                    message: bytes = datum[start_index:(start_index + _TRADE_MESSAGE_SIZE)]
                    symbol: str = message[0:21].decode('ascii')
                    price: float = struct.unpack_from('<d', message, 22)[0]
                    size: int = struct.unpack_from('<L', message, 30)[0]
                    timestamp: float = struct.unpack_from('<d', message, 34)[0]
                    totalVolume: int = struct.unpack_from('<Q', message, 42)[0]
                    on_trade(Trade(symbol, price, size, totalVolume, timestamp))
                    start_index = start_index + _TRADE_MESSAGE_SIZE
                elif msg_type > 2:  # Unusual Activity
                    message: bytes = datum[start_index:(start_index + _UNUSUAL_ACTIVITY_MESSAGE_SIZE)]
                    symbol: str = message[0:21].decode('ascii')
                    type: UnusualActivityType = message[21]
                    sentiment: UnusualActivitySentiment = message[22]
                    totalValue: float = struct.unpack_from('<f', message, 23)[0]
                    totalSize: int = struct.unpack_from('<L', message, 27)[0]
                    averagePrice: float = struct.unpack_from('<f', message, 31)[0]
                    askAtExecution: float = struct.unpack_from('<f', message, 35)[0]
                    bidAtExecution: float = struct.unpack_from('<f', message, 39)[0]
                    priceAtExecution: float = struct.unpack_from('<f', message, 43)[0]
                    timestamp: float = struct.unpack_from('<d', message, 47)[0]
                    if on_unusual_activity: on_unusual_activity(
                        UnusualActivity(symbol, type, sentiment, totalValue, totalSize, averagePrice, askAtExecution,
                                        bidAtExecution, priceAtExecution, timestamp))
                    start_index = start_index + _UNUSUAL_ACTIVITY_MESSAGE_SIZE
                elif msg_type == 2:  # Refresh
                    message: bytes = datum[start_index:(start_index + _REFRESH_MESSAGE_SIZE)]
                    symbol: str = message[0:21].decode('ascii')
                    refresh: int = struct.unpack_from('<i', message, 22)[0]
                    timestamp: float = struct.unpack_from('<d', message, 26)[0]
                    if on_refresh: on_refresh(Refresh(symbol, refresh, timestamp))
                    start_index = start_index + _REFRESH_MESSAGE_SIZE
                else:
                    _log.warn("Invalid Message Type: {0}".format(msg_type))
        except queue.Empty:
            continue
    _log.debug("Worker thread {0} stopped".format(index))


class Client:
    def __init__(self, config: Config, onTrade: Callable[[Trade], None], onQuote: Callable[[Quote], None] = None,
                 onRefresh: Callable[[Refresh], None] = None,
                 onUnusualActivity: Callable[[UnusualActivity], None] = None):
        if not config:
            raise ValueError("Config is required")
        if (not config.apiKey) or (not isinstance(config.apiKey, str)):
            raise ValueError("You must provide a valid API key")
        if (not config.provider) or (not isinstance(config.provider, Providers)):
            raise ValueError("You must specify a valid provider")
        if ((config.provider == Providers.MANUAL) or (config.provider == Providers.MANUAL_FIREHOSE)) and (
                (not config.manualIpAddress) or (not isinstance(config.manualIpAddress, str))):
            raise ValueError("You must specify an IP address for a manual configuration")
        if (onTrade):
            if (callable(onTrade)):
                self.__useOnTrade: bool = True
            else:
                raise ValueError("Parameter 'on_trade' must be a function")
        else:
            self.__useOnTrade: bool = False
        if (onQuote):
            if (callable(onQuote)):
                self.__useOnQuote: bool = True
            else:
                raise ValueError("Parameter 'on_quote' must be a function")
        else:
            self.__useOnQuote: bool = False
        if (onRefresh):
            if (callable(onRefresh)):
                self.__useOnRefresh: bool = True
            else:
                raise ValueError("Parameter 'on_refresh' must be a function")
        else:
            self.__useOnRefresh: bool = False
        if (onUnusualActivity):
            if (callable(onUnusualActivity)):
                self.__useOnUnusualActivity: bool = True
            else:
                raise ValueError("Parameter 'on_unusual_activity' must be a function")
        else:
            self.__useOnUnusualActivity: bool = False
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
        self.__tLock: threading.Lock = threading.Lock()
        self.__wsLock: threading.Lock = threading.Lock()
        self.__heartbeatThread: threading.Thread = threading.Thread(None, _heartbeat_fn,
                                                                    args=[self.__wsLock, self.__webSocket],
                                                                    daemon=True)
        self.__workerThreads: list[threading.Thread] = [threading.Thread(None, _thread_fn,
                                                                         args=[i, self.__data, onTrade, onQuote,
                                                                               onRefresh, onUnusualActivity],
                                                                         daemon=True) for i in range(config.numThreads)]
        self.__socketThread: threading.Thread = None
        self.__isStarted: bool = False
        _log.setLevel(config.logLevel)

    def __allReady(self) -> bool:
        self.__wsLock.acquire()
        ready: bool = True
        try:
            ready = self.__webSocket.isReady
        finally:
            self.__wsLock.release()
        return ready

    def __getAuthUrl(self) -> str:
        if self.__provider == Providers.OPRA:
            return "https://realtime-options.intrinio.com/auth?api_key=" + self.__apiKey
        elif self.__provider == Providers.MANUAL:
            return "http://" + self.__manualIP + "/auth?api_key=" + self.__apiKey
        else:
            raise ValueError("Provider not specified")

    def __getWebSocketUrl(self, token: str) -> str:
        if self.__provider == Providers.OPRA:
            return "wss://realtime-options.intrinio.com/socket/websocket?vsn=1.0.0&token=" + token
        elif self.__provider == Providers.MANUAL:
            return "ws://" + self.__manualIP + "/socket/websocket?vsn=1.0.0&token=" + token
        else:
            raise ValueError("Provider not specified")

    def __trySetToken(self) -> bool:
        _log.info("Authorizing...")
        headers = {"Client-Information": "IntrinioOptionsPythonSDKv1.0"}
        try:
            response: requests.Response = requests.get(self.__getAuthUrl(), headers=headers, timeout=1)
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

    def __getToken(self) -> str:
        self.__tLock.acquire()
        try:
            if ((time.time() - self.__token[1]) > (60 * 60 * 24)):  # 60sec/min * 60min/hr * 24hrs = 1 day
                do_backoff(self.__trySetToken)
            return self.__token[0]
        finally:
            self.__tLock.release()

    def __getChannels(self) -> set[str]:
        return self.__channels

    def __get_option_mask(self) -> int:
        mask: int = 0
        if self.__useOnTrade:
            mask |= 0b0001
        if self.__useOnQuote:
            mask |= 0b0010
        if self.__useOnRefresh:
            mask |= 0b0100
        if self.__useOnUnusualActivity:
            mask |= 0b1000
        return mask

    def __join(self, symbol: str):
        if symbol not in self.__channels:
            self.__channels.add(symbol)
            symbol_bytes = bytes(symbol, 'utf-8')
            message: bytes = bytearray(len(symbol_bytes)+2)
            message[0] = 74  # join code
            message[1] = self.__get_option_mask()
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
            message[1] = self.__get_option_mask()
            message[2:] = symbol_bytes
            if self.__webSocket.isReady:
                _log.info("Websocket - Leaving channel: {0}".format(symbol))
                self.__webSocket.send_binary(message)

    def join(self, *symbols):
        if self.__isStarted:
            while not self.__allReady(): time.sleep(1.0)
        for (symbol) in symbols:
            if (not symbol in self.__channels):
                self.__join(symbol)

    def join_firehose(self):
        if "$FIREHOSE" in self.__channels:
            _log.warn("This client has already joined the firehose channel")
        else:
            if self.__isStarted:
                while not self.__allReady():
                    time.sleep(1.0)
            self.__join("$FIREHOSE")

    def leave(self, *symbols):
        if not symbols:
            _log.info("Leaving all channels")
            channels: set[str] = self.__channels.copy()
            for (symbol) in channels:
                self.__leave(symbol)
        symbolSet: set[str] = set(symbols)
        for sym in symbolSet:
            self.__leave(sym)

    def leave_firehose(self):
        if "$FIREHOSE" in self.__channels:
            self.__leave("$FIREHOSE")

    def __socket_start_fn(self, token: str):
        _log.info("Websocket - Connecting...")
        ws_url: str = self.__getWebSocketUrl(token)
        self.__webSocket = _WebSocket(ws_url,
                                      self.__wsLock,
                                      self.__heartbeatThread,
                                      self.__workerThreads,
                                      self.__getChannels,
                                      self.__getToken,
                                      self.__getWebSocketUrl,
                                      self.__useOnTrade,
                                      self.__useOnQuote,
                                      self.__useOnRefresh,
                                      self.__useOnUnusualActivity,
                                      self.__data)
        self.__webSocket.start()

    def start(self):
        if (not (self.__useOnTrade or self.__useOnQuote or self.__useOnRefresh or self.__useOnUnusualActivity)):
            raise ValueError("You must set at least one callback method before starting client")
        token: str = self.__getToken()
        self.__wsLock.acquire()
        try:
            self.__socketThread = threading.Thread = threading.Thread(None, self.__socket_start_fn, args=[token], daemon=True)
        finally:
            self.__wsLock.release()
        self.__socketThread.start()
        self.__isStarted = True

    def stop(self):
        _log.info("Stopping...")
        if len(self.__channels) > 0:
            self.leave()
        time.sleep(1.0)
        self.__wsLock.acquire()
        try:
            self.__webSocket.isReady = False
        finally:
            self.__wsLock.release()
        _stopFlag.set()
        self.__webSocket.stop()
        for i in range(len(self.__workerThreads)):
            self.__workerThreads[i].join()
            _log.debug("Worker thread {0} joined".format(i))
        self.__socketThread.join()
        _log.debug("Socket thread joined")
        _log.info("Stopped")

    def get_stats(self) -> tuple[int, int, int]:
        return _dataMsgCount, _txtMsgCount, self.__data.qsize()
