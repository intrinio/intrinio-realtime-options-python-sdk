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


def Log(message: str):
    _log.info(message)


def _doBackoff(fn: Callable[[None], bool]):
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
class QuoteType(IntEnum):
    ASK = 1
    BID = 2


@unique
class LogLevel(IntEnum):
    DEBUG = logging.DEBUG
    INFO = logging.INFO


class Quote:
    def __init__(self, symbol: str, type: QuoteType, price: float, size: int, timestamp: float):
        self.symbol: str = symbol
        self.type: QuoteType = type
        self.price: float = price
        self.size: int = size
        self.timestamp: float = timestamp

    def __str__(self) -> str:
        return "Quote (Type: {0}, Symbol: {1}, Price: {2:.2f}, Size: {3}, Timestamp: {4})".format(self.type,
                                                                                                  self.symbol,
                                                                                                  self.price, self.size,
                                                                                                  self.timestamp)

    def getStrikePrice(self) -> float:
        whole: int = (self.symbol[13] - '0') * 10000 + (self.symbol[14] - '0') * 1000 + (
                    self.symbol[15] - '0') * 100 + (self.symbol[16] - '0') * 10 + (self.symbol[17] - '0')
        part: float = (self.symbol[18] - '0') * 0.1 + (self.symbol[19] - '0') * 0.01 + (self.symbol[20] - '0') * 0.001
        return float(whole) + part

    def isPut(self) -> bool:
        return self.symbol[12] == 'P'

    def isCall(self) -> bool:
        return self.symbol[12] == 'C'

    def getExpirationDate(self) -> time.struct_time:
        return time.strptime(self.symbol[6:12], "%y%m%d")

    def getUnderlyingSymbol(self) -> str:
        return self.symbol[0:6].rstrip('_')


class Trade:
    def __init__(self, symbol: str, price: float, size: int, totalVolume: int, timestamp: float):
        self.symbol: str = symbol
        self.price: float = price
        self.size: int = size
        self.totalVolume: int = totalVolume
        self.timestamp: float = timestamp

    def __str__(self) -> str:
        return ("Trade (Symbol: {0}, Price: {1:.2f}, Size: {2}, TotalVolume: {3}, Timestamp: {4})".format(self.symbol,
                                                                                                          self.price,
                                                                                                          self.size,
                                                                                                          self.totalVolume,
                                                                                                          self.timestamp))

    def getStrikePrice(self) -> float:
        whole: int = (self.symbol[13] - '0') * 10000 + (self.symbol[14] - '0') * 1000 + (
                    self.symbol[15] - '0') * 100 + (self.symbol[16] - '0') * 10 + (self.symbol[17] - '0')
        part: float = (self.symbol[18] - '0') * 0.1 + (self.symbol[19] - '0') * 0.01 + (self.symbol[20] - '0') * 0.001
        return float(whole) + part

    def isPut(self) -> bool:
        return self.symbol[12] == 'P'

    def isCall(self) -> bool:
        return self.symbol[12] == 'C'

    def getExpirationDate(self) -> time.struct_time:
        return time.strptime(self.symbol[6:12], "%y%m%d")

    def getUnderlyingSymbol(self) -> str:
        return self.symbol[0:6].rstrip('_')


class OpenInterest:
    def __init__(self, symbol: str, openInterest: int, timestamp: float):
        self.symbol: str = symbol
        self.openInterest: int = openInterest
        self.timestamp: float = timestamp

    def __str__(self) -> str:
        return "OpenInterest (Symbol: {0}, Value: {1}, Timestamp: {2})".format(self.symbol, self.openInterest,
                                                                               self.timestamp)


@unique
class UnusualActivityType(IntEnum):
    BLOCK = 4
    SWEEP = 5
    LARGE = 6


@unique
class UnusualActivitySentiment(IntEnum):
    NEUTRAL = 0
    BULLISH = 1
    BEARISH = 2


class UnusualActivity:
    def __init__(self,
                 symbol: str,
                 type: UnusualActivityType,
                 sentiment: UnusualActivitySentiment,
                 totalValue: float,
                 totalSize: int,
                 averagePrice: float,
                 askAtExecution: float,
                 bidAtExecution: float,
                 priceAtExecution: float,
                 timestamp: float):
        self.symbol: str = symbol
        self.type: UnusualActivityType = type
        self.sentiment: UnusualActivitySentiment = sentiment
        self.totalValue: float = totalValue
        self.totalSize: int = totalSize
        self.averagePrice: float = averagePrice
        self.askAtExecution: float = askAtExecution
        self.bidAtExecution: float = bidAtExecution
        self.priceAtExecution: float = priceAtExecution
        self.timestamp: float = timestamp

    def __str__(self) -> str:
        return "Unusual Activity (Type: {0}, Sentiment: {1}, Symbol: {2}, Total Value: {3:.2f}, Total Size: {4}, Average Price: {5:.2f}, Ask at Execution: {6:.2f}, Bid at Execution: {7:.2f}, Underlying Price at Execution: {8:.2f}, Timestamp: {5})".format(
            self.type, self.sentiment, self.symbol, self.totalValue, self.totalSize, self.averagePrice,
            self.askAtExecution, self.bidAtExecution, self.priceAtExecution, self.timestamp)

    def getStrikePrice(self) -> float:
        whole: int = (self.symbol[13] - '0') * 10000 + (self.symbol[14] - '0') * 1000 + (
                    self.symbol[15] - '0') * 100 + (self.symbol[16] - '0') * 10 + (self.symbol[17] - '0')
        part: float = (self.symbol[18] - '0') * 0.1 + (self.symbol[19] - '0') * 0.01 + (self.symbol[20] - '0') * 0.001
        return float(whole) + part

    def isPut(self) -> bool:
        return self.symbol[12] == 'P'

    def isCall(self) -> bool:
        return self.symbol[12] == 'C'

    def getExpirationDate(self) -> time.struct_time:
        return time.strptime(self.symbol[6:12], "%y%m%d")

    def getUnderlyingSymbol(self) -> str:
        return self.symbol[0:6].rstrip('_')


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
            if (not self.__heartbeatThread.is_alive()):
                self.__heartbeatThread.start()
            for worker in self.__workerThreads:
                if (not worker.is_alive()):
                    worker.start()
        finally:
            self.__wsLock.release()
        if (self.__getChannels) and (callable(self.__getChannels)):
            channels: set[str] = self.__getChannels()
            if (channels) and (len(channels) > 0):
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
                    _doBackoff(self.__tryReconnect)
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


def _heartbeatFn(wsLock: threading.Lock, webSockets: list[_WebSocket]):
    _log.debug("Starting heartbeat")
    while not _stopFlag.is_set():
        time.sleep(_HEARTBEAT_INTERVAL)
        _log.debug("Sending heartbeat")
        wsLock.acquire()
        try:
            for ws in webSockets:
                if (not _stopFlag.is_set()):
                    ws.sendHeartbeat()
        finally:
            wsLock.release()
    _log.debug("Heartbeat stopped")


def _threadFn(index: int, data: queue.Queue, onTrade: Callable[[Trade], None], onQuote: Callable[[Quote], None] = None,
              onOpenInterest: Callable[[OpenInterest], None] = None,
              onUnusualActivity: Callable[[UnusualActivity], None] = None):
    _log.debug("Starting worker thread {0}".format(index))
    while (not _stopFlag.is_set()):
        try:
            datum: bytes = data.get(True, 1.0)
            count: int = datum[0]
            startIndex: int = 1
            for _ in range(count):
                msgType: int = datum[startIndex + 21]
                if (msgType == 1) or (msgType == 2):
                    message: bytes = datum[startIndex:(startIndex + 42)]
                    symbol: str = message[0:21].decode('ascii')
                    type: QuoteType = message[21]
                    price: float = struct.unpack_from('<d', message, 22)[0]
                    size: int = struct.unpack_from('<L', message, 30)[0]
                    timestamp: float = struct.unpack_from('<d', message, 34)[0]
                    if onQuote: onQuote(Quote(symbol, type, price, size, timestamp))
                    startIndex = startIndex + 42
                elif (msgType == 0):
                    message: bytes = datum[startIndex:(startIndex + 50)]
                    symbol: str = message[0:21].decode('ascii')
                    price: float = struct.unpack_from('<d', message, 22)[0]
                    size: int = struct.unpack_from('<L', message, 30)[0]
                    timestamp: float = struct.unpack_from('<d', message, 34)[0]
                    totalVolume: int = struct.unpack_from('<Q', message, 42)[0]
                    onTrade(Trade(symbol, price, size, totalVolume, timestamp))
                    startIndex = startIndex + 50
                elif (msgType > 3):
                    message: bytes = datum[startIndex:(startIndex + 55)]
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
                    if onUnusualActivity: onUnusualActivity(
                        UnusualActivity(symbol, type, sentiment, totalValue, totalSize, averagePrice, askAtExecution,
                                        bidAtExecution, priceAtExecution, timestamp))
                    startIndex = startIndex + 55
                elif (msgType == 3):
                    message: bytes = datum[startIndex:(startIndex + 34)]
                    symbol: str = message[0:21].decode('ascii')
                    openInterest: int = struct.unpack_from('<i', message, 22)[0]
                    timestamp: float = struct.unpack_from('<d', message, 26)[0]
                    if onOpenInterest: onOpenInterest(OpenInterest(symbol, openInterest, timestamp))
                    startIndex = startIndex + 34
                else:
                    _log.warn("Invalid Message Type: {0}".format(msgType))
        except queue.Empty:
            continue
    _log.debug("Worker thread {0} stopped".format(index))


class Client:
    def __init__(self, config: Config, onTrade: Callable[[Trade], None], onQuote: Callable[[Quote], None] = None,
                 onOpenInterest: Callable[[OpenInterest], None] = None,
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
                raise ValueError("Parameter 'onTrade' must be a function")
        else:
            self.__useOnTrade: bool = False
        if (onQuote):
            if (callable(onQuote)):
                self.__useOnQuote: bool = True
            else:
                raise ValueError("Parameter 'onQuote' must be a function")
        else:
            self.__useOnQuote: bool = False
        if (onOpenInterest):
            if (callable(onOpenInterest)):
                self.__useOnOpenInterest: bool = True
            else:
                raise ValueError("Parameter 'onOpenInterest' must be a function")
        else:
            self.__useOnOpenInterest: bool = False
        if (onUnusualActivity):
            if (callable(onUnusualActivity)):
                self.__useOnUnusualActivity: bool = True
            else:
                raise ValueError("Parameter 'onUnusualActivity' must be a function")
        else:
            self.__useOnUnusualActivity: bool = False
        self.__provider: Providers = config.provider
        self.__apiKey: str = config.apiKey
        self.__manualIP: str = config.manualIpAddress
        self.__token: tuple[str, float] = (None, 0.0)
        self.__webSockets: list[_WebSocket] = list()
        if config.symbols and (isinstance(config.symbols, list)) and (len(config.symbols) > 0):
            self.__channels: set[str] = set((symbol) for symbol in config.symbols)
        else:
            self.__channels: set[str] = set()
        self.__data: queue.Queue = queue.Queue()
        self.__tLock: threading.Lock = threading.Lock()
        self.__wsLock: threading.Lock = threading.Lock()
        self.__heartbeatThread: threading.Thread = threading.Thread(None, _heartbeatFn,
                                                                    args=[self.__wsLock, self.__webSockets],
                                                                    daemon=True)
        self.__workerThreads: list[threading.Thread] = [threading.Thread(None, _threadFn,
                                                                         args=[i, self.__data, onTrade, onQuote,
                                                                               onOpenInterest, onUnusualActivity],
                                                                         daemon=True) for i in range(config.numThreads)]
        self.__socketThread: threading.Thread = None
        self.__isStarted: bool = False
        _log.setLevel(config.logLevel)

    def __allReady(self) -> bool:
        self.__wsLock.acquire()
        ready: bool = True
        try:
            for ws in self.__webSockets:
                ready = ready and ws.isReady
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
                _doBackoff(self.__trySetToken)
            return self.__token[0]
        finally:
            self.__tLock.release()

    def __getChannels(self) -> set[str]:
        return self.__channels

    def __join(self, symbol: str):
        if (symbol not in self.__channels):
            self.__channels.add(symbol)
            subscriptionSelection: str = ""
            subscriptionList: list[str] = list()
            if (self.__useOnTrade):
                subscriptionSelection += ",\"trade_data\":\"true\""
                subscriptionList.append("trade")
            if (self.__useOnQuote):
                subscriptionSelection += ",\"quote_data\":\"true\""
                subscriptionList.append("quote")
            if (self.__useOnOpenInterest):
                subscriptionSelection += ",\"open_interest_data\":\"true\""
                subscriptionList.append("open interest")
            if (self.__useOnUnusualActivity):
                subscriptionSelection += ",\"unusual_activity_data\":\"true\""
                subscriptionList.append("unusual activity")
            message: str = "{\"topic\":\"options:" + symbol + "\",\"event\":\"phx_join\"" + subscriptionSelection + ",\"payload\":{},\"ref\":null}"
            for i in range(len(self.__webSockets)):
                if (self.__webSockets[i].isReady):
                    subscriptionSelection = ", ".join(subscriptionList)
                    _log.info("Websocket {0} - Joining channel: {1} (subscriptions = {2})".format(i, symbol,
                                                                                                  subscriptionSelection))
                    self.__webSockets[i].send(message)

    def __leave(self, symbol: str):
        if (symbol in self.__channels):
            self.__channels.remove(symbol)
            message: str = "{\"topic\":\"options:" + symbol + "\",\"event\":\"phx_leave\",\"payload\":{},\"ref\":null}"
            for i in range(len(self.__webSockets)):
                if (self.__webSockets[i].isReady):
                    _log.info("Websocket {0} - Leaving channel: {1}".format(i, symbol))
                    self.__webSockets[i].send(message)

    def join(self, *symbols):
        if self.__isStarted:
            while not self.__allReady(): time.sleep(1.0)
        for (symbol) in symbols:
            if (not symbol in self.__channels):
                self.__join(symbol)

    def joinFirehose(self):
        if ("$FIREHOSE" in self.__channels):
            _log.warn("This client has already joined the firehose channel")
        else:
            if self.__isStarted:
                while not self.__allReady(): time.sleep(1.0)
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

    def leaveFirehose(self):
        if ("$FIREHOSE" in self.__channels):
            self.__leave("$FIREHOSE")

    def __socketStartFn(self, token: str):
        _log.info("Websocket - Connecting...")
        wsUrl: str = self.__getWebSocketUrl(token)
        ws: _WebSocket = _WebSocket(wsUrl,
                                    self.__wsLock,
                                    self.__heartbeatThread,
                                    self.__workerThreads,
                                    self.__getChannels,
                                    self.__getToken,
                                    self.__getWebSocketUrl,
                                    self.__useOnTrade,
                                    self.__useOnQuote,
                                    self.__useOnOpenInterest,
                                    self.__useOnUnusualActivity,
                                    self.__data)
        self.__webSockets.append(ws)
        ws.start()

    def start(self):
        if (not (self.__useOnTrade or self.__useOnQuote or self.__useOnOpenInterest or self.__useOnUnusualActivity)):
            raise ValueError("You must set at least one callback method before starting client")
        token: str = self.__getToken()
        self.__wsLock.acquire()
        try:
            self.__socketThread = threading.Thread = threading.Thread(None, self.__socketStartFn, args=[token], daemon=True)
        finally:
            self.__wsLock.release()
        self.__socketThread.start()
        self.__isStarted = True

    def stop(self):
        _log.info("Stopping...")
        if (len(self.__channels) > 0):
            self.leave()
        time.sleep(1.0)
        self.__wsLock.acquire()
        try:
            for ws in self.__webSockets:
                ws.isReady = False
        finally:
            self.__wsLock.release()
        _stopFlag.set()
        for ws in self.__webSockets:
            ws.stop()
        for i in range(len(self.__workerThreads)):
            self.__workerThreads[i].join()
            _log.debug("Worker thread {0} joined".format(i))
        self.__socketThread.join()
        _log.debug("Socket thread joined")
        _log.info("Stopped")

    def getStats(self) -> tuple[int, int, int]:
        return (_dataMsgCount, _txtMsgCount, self.__data.qsize())
