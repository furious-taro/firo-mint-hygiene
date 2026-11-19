"""A fake firod JSON-RPC server for offline tests.

It speaks HTTP + JSON-RPC 1.0 with Basic auth like firod, and its wallet methods follow the
rules in firo src/wallet/rpcwallet.cpp and src/spark/sparkwallet.cpp (v0.14.18.2):

* mintspark(recipients_object, subtractFeeFromAmount, [fromAddresses]): recipients is an object
  keyed by Spark address with {"amount", "memo"}; fromAddresses selects every unlocked output at
  those addresses; when the selected group is minted in full and the fee does not fit, the fee is
  taken from the mint value, which comes off the LAST output (CreateSparkMintTransactions).
* locked coins (lockunspent) are invisible to listunspent and to coin selection.
* listsparkmints refuses with -13 while an encrypted wallet is locked.
"""

import base64
import json
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COIN = 100_000_000


def sats(v):
    return int(Decimal(str(v)) * COIN)


def amt(s):
    return Decimal(s) / COIN


class RpcFail(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


class FakeWallet:
    def __init__(self, chain="main", height=1400000):
        self.chain = chain
        self.height = height
        self.txs = {}          # txid -> {"vin": [...], "vout": [...], "height": h, "generated": bool, "time": t}
        self.mine = set()      # transparent addresses owned by the wallet
        self.locked = set()    # (txid, n) locked via lockunspent
        self.spark_addresses = {"0": "sm1default"}
        self.spark_coins = []  # {"txid", "nHeight", "amount"}
        self.spark_spends = []  # {"txid", "lTagHash", "lTag", "amount"}
        self.encrypted = False
        self.unlocked = True
        self.calls = []
        self.fee = 30_000      # fee charged for any mint (sats)
        self._n = 0

    # ---- building history
    def _txid(self):
        self._n += 1
        return "%064x" % self._n

    def add_tx(self, vin, vout, height=None, generated=False, time_=None):
        txid = self._txid()
        h = self.height if height is None else height
        self.txs[txid] = {"vin": vin, "vout": vout, "height": h, "generated": generated,
                          "time": time_ if time_ is not None else 1_760_000_000 + h * 300}
        return txid

    def receive(self, address, value, height=None, generated=False, time_=None):
        self.mine.add(address)
        vin = [{"coinbase": "03ab"}] if generated else [{"txid": "ff" * 32, "vout": 0}]
        return self.add_tx(vin, [{"address": address, "sats": sats(value)}], height, generated, time_)

    def spent(self, txid, n):
        for t in self.txs.values():
            for v in t["vin"]:
                if v.get("txid") == txid and v.get("vout") == n:
                    return True
        return False

    def utxos(self):
        for txid, t in self.txs.items():
            for n, o in enumerate(t["vout"]):
                if o.get("address") in self.mine and not o.get("spark") and not self.spent(txid, n):
                    yield txid, n, o, t

    def mint(self, inputs, values, height=None, time_=None):
        """Record a mint made in the past (for audit tests)."""
        vin = [{"txid": a, "vout": b} for a, b in inputs]
        vout = [{"spark": "d1", "sats": sats(v)} for v in values]
        txid = self.add_tx(vin, vout, height, time_=time_)
        h = self.txs[txid]["height"]
        for v in values:
            self.spark_coins.append({"txid": txid, "nHeight": h, "amount": Decimal(str(v))})
        return txid

    def spark_spend(self, n_coins, transparent, height=None):
        """A Spark spend consuming n coins, paying `transparent` [(address, value)] publicly."""
        vout = [{"address": a, "sats": sats(v)} for a, v in transparent] + [{"spark": "d2", "sats": 0}]
        txid = self.add_tx([{"sparkspend": True}], vout, height)
        for i in range(n_coins):
            self.spark_spends.append({"txid": txid, "lTagHash": "%064x" % i, "lTag": "00", "amount": 1})
        return txid

    def send(self, inputs, outputs, height=None):
        vin = [{"txid": a, "vout": b} for a, b in inputs]
        vout = [{"address": a, "sats": sats(v)} for a, v in outputs]
        return self.add_tx(vin, vout, height)

    # ---- RPC
    def dispatch(self, method, params):
        self.calls.append((method, params))
        fn = getattr(self, "rpc_" + method, None)
        if fn is None:
            raise RpcFail(-32601, "Method not found")
        return fn(*params)

    def rpc_getblockchaininfo(self):
        return {"chain": self.chain, "blocks": self.height}

    def rpc_getblockcount(self):
        return self.height

    def rpc_listunspent(self, minconf=1, maxconf=9999999, addresses=None):
        res = []
        for txid, n, o, t in self.utxos():
            conf = self.height - t["height"] + 1
            if (txid, n) in self.locked or conf < minconf or conf > maxconf:
                continue
            if t["generated"] and conf < 100:
                continue
            if addresses and o["address"] not in addresses:
                continue
            res.append({"txid": txid, "vout": n, "address": o["address"], "amount": amt(o["sats"]),
                        "confirmations": conf, "spendable": True, "solvable": True})
        return res

    def rpc_gettransaction(self, txid, include_watchonly=False):
        t = self.txs.get(txid)
        mine = t and (any(o.get("address") in self.mine for o in t["vout"]) or
                      any(v.get("txid") in self.txs for v in t["vin"]) or
                      any(o.get("spark") for o in t["vout"]))
        if not mine:
            raise RpcFail(-5, "Invalid or non-wallet transaction id")
        res = {"txid": txid, "confirmations": self.height - t["height"] + 1, "time": t["time"],
               "blocktime": t["time"], "hex": "raw:" + txid, "details": []}
        if t["generated"]:
            res["generated"] = True
        return res

    def rpc_decoderawtransaction(self, hexstr):
        txid = hexstr[4:]
        t = self.txs[txid]
        vin = []
        for v in t["vin"]:
            if "coinbase" in v:
                vin.append({"coinbase": v["coinbase"], "sequence": 4294967295})
            elif "txid" in v:
                vin.append({"txid": v["txid"], "vout": v["vout"], "scriptSig": {"hex": "47"}})
            else:
                vin.append({"scriptSig": {"hex": "d3"}, "sequence": 4294967295})
        vout = []
        for n, o in enumerate(t["vout"]):
            if o.get("spark"):
                spk = {"hex": o["spark"] + "00ff", "type": "nonstandard"}
            else:
                spk = {"hex": "76a914" + "00" * 20 + "88ac", "type": "pubkeyhash", "addresses": [o["address"]]}
            vout.append({"value": amt(o["sats"]), "n": n, "scriptPubKey": spk})
        return {"txid": txid, "vin": vin, "vout": vout}

    def rpc_listtransactions(self, account="*", count=10, skip=0, watch=False):
        res = []
        for txid, t in self.txs.items():
            if any(v.get("txid") in self.txs for v in t["vin"]):
                res.append({"txid": txid, "category": "send"})
        return res

    def rpc_getallsparkaddresses(self):
        return dict(self.spark_addresses)

    def rpc_getnewsparkaddress(self):
        k = str(len(self.spark_addresses))
        self.spark_addresses[k] = "sm1new%s" % k
        return [self.spark_addresses[k]]

    def rpc_lockunspent(self, unlock, outs):
        for o in outs:
            key = (o["txid"], o["vout"])
            if unlock:
                self.locked.discard(key)
            else:
                self.locked.add(key)
        return True

    def rpc_walletpassphrase(self, pw, timeout):
        if pw != "correct horse":
            raise RpcFail(-14, "Error: The wallet passphrase entered was incorrect.")
        self.unlocked = True
        return None

    def rpc_walletlock(self):
        self.unlocked = False
        return None

    def _need_unlocked(self):
        if self.encrypted and not self.unlocked:
            raise RpcFail(-13, "Error: Please enter the wallet passphrase with walletpassphrase first.")

    def rpc_listsparkmints(self):
        self._need_unlocked()
        return [{"txid": c["txid"], "nHeight": c["nHeight"], "nId": 1, "isUsed": False,
                 "lTagHash": "00", "memo": "", "scriptPubKey": "d1", "amount": c["amount"]}
                for c in self.spark_coins]

    def rpc_listsparkspends(self):
        return list(self.spark_spends)

    def rpc_mintspark(self, recipients, subtract_fee=False, from_addresses=None):
        self._need_unlocked()
        if not isinstance(recipients, dict) or not recipients:
            raise RpcFail(-1, "JSON value is not an object as expected")
        if not isinstance(subtract_fee, bool):
            raise RpcFail(-1, "JSON value is not a boolean as expected")
        outputs = []
        for addr, spec in recipients.items():
            if not addr.startswith("sm1"):
                raise RpcFail(-5, "Invalid Spark address: " + addr)
            if "amount" not in spec:
                raise RpcFail(-8, "Invalid parameters, no amount: " + addr)
            a = spec["amount"]
            if not isinstance(a, (str, int, Decimal)):
                raise RpcFail(-3, "Amount is not a number or string")
            d = Decimal(str(a))
            if d * COIN != int(d * COIN):
                raise RpcFail(-3, "Invalid amount")
            if d <= 0:
                raise RpcFail(-3, "Invalid amount for send")
            outputs.append([addr, int(d * COIN)])
        group = [(txid, n, o) for txid, n, o, t in self.utxos()
                 if (txid, n) not in self.locked
                 and (not from_addresses or o["address"] in from_addresses)]
        balance = sum(o["sats"] for _, _, o in group)
        want = sum(v for _, v in outputs)
        if want > balance:
            raise RpcFail(-4, "Insufficient funds")
        if subtract_fee:
            raise RpcFail(-4, "fake node: subtractFeeFromAmount not modelled")
        if want + self.fee <= balance:
            change = balance - want - self.fee   # simplification: whole group is spent
        else:
            # "if no enough coins in this group then subtract fee from mint": allocate the reduced
            # mint value to outputs in order, so the last output absorbs the fee.
            change = 0
            remaining = want - self.fee
            for o in outputs:
                o[1] = min(o[1], remaining)
                remaining -= o[1]
            if outputs[-1][1] <= 0:
                raise RpcFail(-4, "Transaction amount too small")
        vin = [{"txid": txid, "vout": n} for txid, n, _ in group]
        vout = [{"spark": "d1", "sats": v, "to": a} for a, v in outputs]
        if change > 0:
            self.mine.add("change-address")
            vout.append({"address": "change-address", "sats": change})
        txid = self.add_tx(vin, vout)
        for _, v in outputs:
            self.spark_coins.append({"txid": txid, "nHeight": self.height, "amount": amt(v)})
        return [txid]


class FakeNode:
    """Run FakeWallet behind a real HTTP server on 127.0.0.1 (random port)."""

    def __init__(self, wallet=None, user="u", password="p"):
        self.wallet = wallet or FakeWallet()
        self.auth = "Basic " + base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        node = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                if self.headers.get("Authorization") != node.auth:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])), parse_float=Decimal)
                try:
                    result = node.wallet.dispatch(req["method"], req.get("params", []))
                    body, status = {"result": result, "error": None, "id": req.get("id")}, 200
                except RpcFail as e:
                    status = 404 if e.code == -32601 else 500
                    body = {"result": None, "error": {"code": e.code, "message": e.msg}, "id": req.get("id")}
                # firod prints amounts as bare decimals with 8 places; emit them without float rounding
                data =json.dumps(body, default=_dec).encode().replace(b'"__D__', b"").replace(b'__D__"', b"")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d/" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _dec(d):
    if isinstance(d, Decimal):
        return "__D__%s__D__" % format(d.quantize(Decimal("0.00000001")), "f")
    raise TypeError(repr(d))
