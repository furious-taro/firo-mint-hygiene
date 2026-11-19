#!/usr/bin/env python3
"""firo-mint-hygiene: move transparent FIRO into Spark without the usual linkable patterns.

Three commands, all talking to your own firod over JSON-RPC:

  plan   read your transparent coins and write a mint schedule (one transparent
         address per mint, common coin values, randomized spacing in time)
  audit  look through your wallet's Spark history for patterns an observer can link
  run    carry out the next step of a plan (dry run unless --execute is given)

Standard library only. The tool never sees private keys: signing stays in firod.
"""

import argparse
import base64
import getpass
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

__version__ = "0.1.0"

COIN = 100_000_000
COLLATERAL_SATS = 1000 * COIN  # masternode collateral: never mint it by accident
DEFAULT_LADDER = ["10000", "5000", "2000", "1000", "500", "200", "100", "50", "20", "10",
                  "5", "2", "1", "0.5", "0.2", "0.1"]
# rpc port and datadir subfolder per network (firo src/chainparamsbase.cpp)
NETWORKS = {
    "main": (8888, ""),
    "test": (18888, "testnet3"),
    "regtest": (28888, "regtest"),
}
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


class ToolError(Exception):
    """A problem the user can fix; printed without a traceback."""


# --------------------------------------------------------------------------- amounts

def to_sats(value):
    """Exact conversion of an RPC amount (Decimal, str, int) to satoshis."""
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ToolError("not an amount: %r" % (value,))
    sats = d * COIN
    if sats != sats.to_integral_value():
        raise ToolError("amount has more than 8 decimals: %s" % value)
    return int(sats)


def fmt(sats):
    """Satoshis to an 8-decimal FIRO string (firod's AmountFromValue accepts strings)."""
    sign = "-" if sats < 0 else ""
    sats = abs(int(sats))
    return "%s%d.%08d" % (sign, sats // COIN, sats % COIN)


def short(sats):
    """Human-friendly amount: trailing zeros removed."""
    s = fmt(sats).rstrip("0")
    return s[:-1] if s.endswith(".") else s


# --------------------------------------------------------------------------- RPC

class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__("RPC error %s: %s" % (code, message))
        self.code = code
        self.message = message


def default_datadir():
    home = os.path.expanduser("~")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        candidates = [os.path.join(base, "firo"), os.path.join(base, "zcoin")]
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
        candidates = [os.path.join(base, "firo"), os.path.join(base, "zcoin")]
    else:
        candidates = [os.path.join(home, ".firo"), os.path.join(home, ".zcoin")]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


def read_conf(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return values


def resolve_rpc(args):
    """Work out URL and credentials the same way firo-cli does. Returns (url, auth, where)."""
    datadir = args.datadir or default_datadir()
    conf = read_conf(args.conf or os.path.join(datadir, "firo.conf"))
    network = args.network
    if network is None:
        if conf.get("regtest") == "1":
            network = "regtest"
        elif conf.get("testnet") == "1":
            network = "test"
        else:
            network = "main"
    port_default, subdir = NETWORKS[network]
    host = args.rpcconnect or conf.get("rpcconnect") or "127.0.0.1"
    port = args.rpcport or conf.get("rpcport") or port_default
    url = "http://%s:%s/" % (host, port)
    user = args.rpcuser or conf.get("rpcuser")
    password = args.rpcpassword or conf.get("rpcpassword")
    if user and password:
        return url, "%s:%s" % (user, password), "rpcuser from %s" % ("command line" if args.rpcuser else "firo.conf")
    cookie = os.path.join(datadir, subdir, ".cookie") if subdir else os.path.join(datadir, ".cookie")
    try:
        with open(cookie, encoding="utf-8") as f:
            return url, f.read().strip(), "cookie " + cookie
    except FileNotFoundError:
        raise ToolError("no RPC credentials: set rpcuser/rpcpassword in firo.conf or pass "
                        "--rpcuser/--rpcpassword, or start firod so it writes %s" % cookie)


class RpcClient:
    def __init__(self, url, auth, timeout=120):
        self.url = url
        self._auth = "Basic " + base64.b64encode(auth.encode()).decode()
        self.timeout = timeout
        self._id = 0
        self._tx_cache = {}

    def call(self, method, *params):
        self._id += 1
        body = json.dumps({"jsonrpc": "1.0", "id": self._id, "method": method,
                           "params": list(params)}).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json", "Authorization": self._auth})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RpcError(-401, "authentication failed (check rpcuser/rpcpassword or the .cookie file)")
            raw = e.read()
            if not raw:
                raise RpcError(-e.code, "HTTP %s from firod" % e.code)
        except urllib.error.URLError as e:
            raise ToolError("cannot reach firod at %s (%s). Is it running with server=1?" % (self.url, e.reason))
        try:
            data = json.loads(raw, parse_float=Decimal)
        except ValueError:
            raise RpcError(-32700, "firod sent something that is not JSON")
        if data.get("error"):
            err = data["error"]
            raise RpcError(err.get("code"), err.get("message"))
        return data["result"]

    def tx(self, txid):
        """gettransaction + decoded body, cached. None if the tx is not in this wallet."""
        if txid not in self._tx_cache:
            try:
                wtx = self.call("gettransaction", txid, True)
            except RpcError as e:
                if e.code == -5:  # Invalid or non-wallet transaction id
                    self._tx_cache[txid] = None
                    return None
                raise
            wtx["decoded"] = self.call("decoderawtransaction", wtx["hex"])
            self._tx_cache[txid] = wtx
        return self._tx_cache[txid]


# --------------------------------------------------------------------------- plan

def split_amount(total, style, ladder, max_coins, fee_reserve, rng):
    """Split `total` sats into Spark coin values. The LAST value is the remainder coin:
    it is never a round value and it pays the network fee (see README, "How the fee is paid")."""
    if max_coins < 1:
        raise ToolError("--max-coins must be at least 1")
    budget = total - fee_reserve
    coins = []
    if style == "common":
        # greedy: largest common values first, so the odd remainder stays small
        for d in ladder:
            while d <= budget and len(coins) < max_coins - 1:
                coins.append(d)
                budget -= d
        # then, at random, break a coin into smaller common values while there is room,
        # so two equal balances do not always produce the same set of coins
        while rng.random() < 0.5:
            breakable = [c for c in coins if c > ladder[-1]]
            if not breakable:
                break
            c = rng.choice(breakable)
            parts, rest = [], c
            for d in ladder:
                while d < c and d <= rest:
                    parts.append(d)
                    rest -= d
            if rest or len(coins) - 1 + len(parts) > max_coins - 1:
                break
            coins.remove(c)
            coins.extend(parts)
    elif style == "random":
        smallest = ladder[-1]
        parts = min(max_coins - 1, budget // max(smallest, 1))
        if parts >= 1:
            n = rng.randint(1, parts)
            cuts = sorted(rng.sample(range(1, max(budget, n + 1)), n))
            prev = 0
            for c in cuts:
                value = c - prev
                value -= value % 10_000  # 4 decimals: odd, but not 8-digit noise
                if value >= smallest:
                    coins.append(value)
                    prev += value
            budget = total - fee_reserve - sum(coins)
    else:
        raise ToolError("unknown --style %r" % style)
    rng.shuffle(coins)
    return coins + [total - sum(coins)]


def circular_hour_gap(a, b):
    d = abs(a - b) % 24
    return min(d, 24 - d)


def schedule(steps, start, min_gap_h, max_gap_h, rng, start_now=False):
    """Give each step a not_before time: random gaps, never two steps in the same
    hour-of-day band back to back (a fixed daily rhythm is measurable, see t4388)."""
    t = start
    last_hour = None
    for i, step in enumerate(steps):
        if i == 0 and start_now:
            cand = start
        else:
            for _ in range(50):
                cand = t + rng.uniform(min_gap_h, max_gap_h) * 3600
                hour = time.gmtime(cand).tm_hour
                if last_hour is None or circular_hour_gap(hour, last_hour) >= 2:
                    break
        step["not_before"] = int(cand)
        t = cand
        last_hour = time.gmtime(cand).tm_hour


def build_plan(rpc, opts, rng=None, now=None):
    rng = rng or random.SystemRandom()
    now = int(now if now is not None else time.time())
    ladder = sorted((to_sats(x) for x in opts.ladder), reverse=True)
    chain = rpc.call("getblockchaininfo")["chain"]
    height = rpc.call("getblockcount")
    utxos = rpc.call("listunspent", 0, 9999999)

    warnings, skipped = [], []
    by_addr = {}
    for u in utxos:
        sats = to_sats(u["amount"])
        if u.get("spendable") is False:
            skipped.append({"what": "%s:%s" % (u["txid"], u["vout"]), "amount": fmt(sats),
                            "why": "watch-only, not spendable by this wallet"})
            continue
        if u.get("confirmations", 0) < opts.minconf:
            skipped.append({"what": "%s:%s" % (u["txid"], u["vout"]), "amount": fmt(sats),
                            "why": "only %s confirmations (need %s)" % (u.get("confirmations", 0), opts.minconf)})
            continue
        by_addr.setdefault(u.get("address", "?"), []).append(u)

    steps = []
    for addr in sorted(by_addr):
        us = by_addr[addr]
        usable, collateral = [], []
        for u in us:
            (collateral if to_sats(u["amount"]) == COLLATERAL_SATS and not opts.include_1000 else usable).append(u)
        if collateral:
            warnings.append("%s holds %d output(s) of exactly 1000 FIRO (masternode collateral?). "
                            "They are left out and will be locked with lockunspent while this "
                            "address is minted. Use --include-1000 if they are not collateral." % (addr, len(collateral)))
        total = sum(to_sats(u["amount"]) for u in usable)
        if not usable:
            continue
        min_mint = to_sats(opts.min_mint)
        if total < min_mint:
            skipped.append({"what": addr, "amount": fmt(total),
                            "why": "below --min-mint %s; minting it would cost a fee for a coin "
                                   "that stands out" % opts.min_mint})
            continue
        coinbase = 0
        for u in usable:
            wtx = rpc.tx(u["txid"])
            if wtx and wtx.get("generated"):
                coinbase += 1
        fee_reserve = max(to_sats(opts.fee_reserve), len(usable) * 20_000)
        if total <= fee_reserve:
            skipped.append({"what": addr, "amount": fmt(total), "why": "smaller than the fee reserve"})
            continue
        outputs = split_amount(total, opts.style, ladder, opts.max_coins, fee_reserve, rng)
        step = {
            "address": addr,
            "outpoints": [{"txid": u["txid"], "vout": u["vout"], "amount": fmt(to_sats(u["amount"]))}
                          for u in usable],
            "total": fmt(total),
            "outputs": [fmt(v) for v in outputs],
            "coinbase_inputs": coinbase,
            "status": "pending",
        }
        if len(usable) > 400:
            warnings.append("%s has %d outputs; one mint may exceed the 250 kB transaction limit. "
                            "Run `firo-cli consolidateaddress` style consolidation on that address "
                            "first (it stays one address, so it adds no new link)." % (addr, len(usable)))
        if coinbase:
            warnings.append("%s is funded by %d mining/masternode reward output(s). Reward handling was "
                            "the largest source of links in the 2026 Spark audit: never send Spark "
                            "withdrawals back to this address or merge them with its coins." % (addr, coinbase))
        if outputs[-1] >= COIN:
            warnings.append("%s: the remainder coin (%s FIRO) has a value only this deposit has. Spend it "
                            "inside Spark (combine it in a private send) before any withdrawal."
                            % (addr, short(outputs[-1])))
        steps.append(step)

    rng.shuffle(steps)
    for i, s in enumerate(steps, 1):
        s["id"] = i
    schedule(steps, now, opts.min_gap_hours, opts.max_gap_hours, rng, opts.start_now)
    if len(steps) > 1:
        warnings.insert(0, "%d transparent addresses. 'Anonymize all' / automintspark would mint all of "
                           "them in the same block, which links them. This plan spaces them %s-%s hours "
                           "apart." % (len(steps), opts.min_gap_hours, opts.max_gap_hours))
    return {
        "tool": "firo-mint-hygiene",
        "version": __version__,
        "created": now,
        "chain": chain,
        "height": height,
        "settings": {"style": opts.style, "max_coins": opts.max_coins, "minconf": opts.minconf,
                     "min_gap_hours": opts.min_gap_hours, "max_gap_hours": opts.max_gap_hours},
        "steps": steps,
        "warnings": warnings,
        "skipped": skipped,
    }


def iso(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))


def print_plan(plan, out=sys.stdout):
    w = out.write
    w("Mint plan (%s, height %s), %d step(s)\n\n" % (plan["chain"], plan["height"], len(plan["steps"])))
    for s in plan["steps"]:
        w("Step %d  not before %s  [%s]\n" % (s["id"], iso(s["not_before"]), s["status"]))
        w("  from   %s  (%d output(s), %s FIRO%s)\n" % (
            s["address"], len(s["outpoints"]), short(to_sats(s["total"])),
            ", %d reward output(s)" % s["coinbase_inputs"] if s["coinbase_inputs"] else ""))
        coins = [short(to_sats(v)) for v in s["outputs"]]
        w("  coins  %s + remainder %s (pays the fee)\n" % (", ".join(coins[:-1]) or "-", coins[-1]))
    if plan["warnings"]:
        w("\nWarnings:\n")
        for x in plan["warnings"]:
            w("  ! %s\n" % x)
    if plan["skipped"]:
        w("\nLeft out:\n")
        for x in plan["skipped"]:
            w("  - %s (%s FIRO): %s\n" % (x["what"], short(to_sats(x["amount"])), x["why"]))


# --------------------------------------------------------------------------- run

def spark_addresses(rpc, n, create):
    """n distinct Spark addresses of this wallet (mintspark takes an object keyed by address).
    Spark recipients are not visible on chain, so reusing your own addresses leaks nothing."""
    existing = rpc.call("getallsparkaddresses")
    addrs = [existing[k] for k in sorted(existing, key=lambda k: int(k))]
    while len(addrs) < n:
        if not create:
            addrs.append("<new Spark address %d>" % (len(addrs) + 1))
            continue
        res = rpc.call("getnewsparkaddress")
        addrs.append(res[0] if isinstance(res, list) else res)
    return addrs[:n]


def save_plan(path, plan):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    os.replace(tmp, path)


def reschedule_after(plan, done_at, min_gap_h):
    """If the user ran late, do not let overdue steps fire back to back: push them out."""
    pending = sorted((s for s in plan["steps"] if s["status"] == "pending"), key=lambda s: s["not_before"])
    if not pending:
        return
    earliest = int(done_at + min_gap_h * 3600)
    if pending[0]["not_before"] < earliest:
        shift = earliest - pending[0]["not_before"]
        for s in pending:
            s["not_before"] += shift


def run_step(rpc, plan, plan_path, args, now=None, out=sys.stdout, ask=input, askpass=getpass.getpass):
    w = out.write
    now = int(now if now is not None else time.time())
    chain = rpc.call("getblockchaininfo")["chain"]
    if chain != plan["chain"]:
        raise ToolError("this plan is for '%s' but firod is on '%s'" % (plan["chain"], chain))
    pending = [s for s in plan["steps"] if s["status"] == "pending"]
    if not pending:
        w("All steps are done.\n")
        return 0
    if args.step is not None:
        matches = [s for s in plan["steps"] if s["id"] == args.step]
        if not matches or matches[0]["status"] != "pending":
            raise ToolError("step %s is not pending" % args.step)
        step = matches[0]
    else:
        step = min(pending, key=lambda s: s["not_before"])
        if step["not_before"] > now and not args.now:
            w("Next step (%d) is not due until %s. Nothing to do yet.\n" % (step["id"], iso(step["not_before"])))
            return 0

    addr = step["address"]
    planned = {(o["txid"], o["vout"]) for o in step["outpoints"]}
    current = rpc.call("listunspent", 0, 9999999, [addr])
    current_keys = {(u["txid"], u["vout"]) for u in current}
    missing = planned - current_keys
    if missing:
        raise ToolError("step %d: %d planned output(s) at %s were spent or locked since planning. "
                        "Run `plan` again." % (step["id"], len(missing), addr))
    extra = [{"txid": u["txid"], "vout": u["vout"]} for u in current if (u["txid"], u["vout"]) not in planned]
    values = [to_sats(v) for v in step["outputs"]]
    recipients_addrs = spark_addresses(rpc, len(values), create=args.execute)
    recipients = {}
    for a, v in zip(recipients_addrs, values):
        recipients[a] = {"amount": fmt(v), "memo": ""}

    w("Step %d: mint %s FIRO from %s into %d Spark coin(s)\n" % (
        step["id"], short(to_sats(step["total"])), addr, len(values)))
    w("  coins: %s (last one is the remainder; the network fee comes out of it)\n"
      % ", ".join(short(v) for v in values))
    if extra:
        w("  %d other output(s) at this address (new funds or collateral) will be locked during the "
          "mint and unlocked right after, so they are not swept in.\n" % len(extra))
    w("  RPC: mintspark %s false [\"%s\"]\n" % (json.dumps(recipients), addr))

    if not args.execute:
        w("DRY RUN: nothing was sent. Add --execute to broadcast this step.\n")
        return 0
    if not args.yes:
        answer = ask("Type MINT to broadcast step %d: " % step["id"])
        if answer.strip() != "MINT":
            w("Not confirmed. Nothing was sent.\n")
            return 1

    unlocked = False
    locked = False
    try:
        if args.unlock:
            rpc.call("walletpassphrase", askpass("Wallet passphrase: "), 120)
            unlocked = True
        if extra:
            rpc.call("lockunspent", False, extra)
            locked = True
        txids = rpc.call("mintspark", recipients, False, [addr])
    finally:
        if locked:
            rpc.call("lockunspent", True, extra)
        if unlocked:
            rpc.call("walletlock")
    step["status"] = "done"
    step["txids"] = txids
    step["executed_at"] = now
    reschedule_after(plan, now, plan["settings"]["min_gap_hours"])
    save_plan(plan_path, plan)
    w("Broadcast: %s\n" % ", ".join(txids))
    nxt = [s for s in plan["steps"] if s["status"] == "pending"]
    if nxt:
        s = min(nxt, key=lambda s: s["not_before"])
        w("Next step %d is due after %s. Run this command again then.\n" % (s["id"], iso(s["not_before"])))
    else:
        w("That was the last step.\n")
    return 0


# --------------------------------------------------------------------------- audit

def _outputs(wtx):
    res = []
    for o in wtx["decoded"]["vout"]:
        spk = o.get("scriptPubKey", {})
        hexs = spk.get("hex", "")
        res.append({
            "n": o["n"], "sats": to_sats(o["value"]), "hex": hexs,
            "address": (spk.get("addresses") or [None])[0],
            "spark": hexs[:2] in ("d1", "d2"),  # OP_SPARKMINT / OP_SPARKSMINT
        })
    return res


def _height(wtx, tip):
    conf = wtx.get("confirmations", 0)
    return tip - conf + 1 if conf and conf > 0 else None


def audit(rpc, opts):
    """Return a list of findings about this wallet's own Spark history."""
    tip = rpc.call("getblockcount")
    ladder = {to_sats(x) for x in DEFAULT_LADDER}
    try:
        coins = rpc.call("listsparkmints")
    except RpcError as e:
        if e.code == -13:
            raise ToolError("the wallet is locked; listsparkmints needs it unlocked. Re-run with --unlock.")
        raise
    spends = rpc.call("listsparkspends")
    spend_count = {}
    for s in spends:
        spend_count[s["txid"]] = spend_count.get(s["txid"], 0) + 1
    spend_txids = set(spend_count)

    def prevout(txid, n):
        wtx = rpc.tx(txid)
        if wtx is None:
            return None
        for o in _outputs(wtx):
            if o["n"] == n:
                o["txid"] = txid
                o["generated"] = bool(wtx.get("generated"))
                o["height"] = _height(wtx, tip)
                o["from_spend"] = txid in spend_txids
                return o
        return None

    # own mint transactions: carry Spark mint outputs, are not spends, and spend our transparent coins
    mints = []
    for txid in sorted({c["txid"] for c in coins} - spend_txids):
        wtx = rpc.tx(txid)
        if wtx is None:
            continue
        ins = []
        for vin in wtx["decoded"]["vin"]:
            if "txid" in vin:
                p = prevout(vin["txid"], vin["vout"])
                if p is not None:
                    ins.append(p)
        if not ins:
            continue  # minted to us by someone else
        mints.append({
            "txid": txid, "height": _height(wtx, tip), "time": wtx.get("blocktime") or wtx.get("time"),
            "inputs": ins, "addresses": sorted({p["address"] for p in ins if p["address"]}),
            "values": [o["sats"] for o in _outputs(wtx) if o["hex"].startswith("d1")],
        })
    mints.sort(key=lambda m: (m["height"] or tip + 1, m["txid"]))

    withdrawals = []
    for txid in sorted(spend_txids):
        wtx = rpc.tx(txid)
        if wtx is None:
            continue
        for o in _outputs(wtx):
            if not o["spark"] and o["address"]:
                withdrawals.append({"txid": txid, "n": o["n"], "address": o["address"], "sats": o["sats"],
                                    "height": _height(wtx, tip)})

    findings = []

    def add(sev, code, title, detail, txids, advice):
        findings.append({"severity": sev, "code": code, "title": title, "detail": detail,
                         "txids": txids, "advice": advice})

    mint_sources = {}
    for m in mints:
        for a in m["addresses"]:
            mint_sources.setdefault(a, []).append(m["txid"])
    reward_addrs = {p["address"] for m in mints for p in m["inputs"] if p["generated"]}

    # R1 one mint spending coins from several transparent addresses
    for m in mints:
        if len(m["addresses"]) > 1:
            add("HIGH", "multi-address-mint", "Mint combined coins from %d transparent addresses" % len(m["addresses"]),
                "Mint %s spends coins from %s. Anyone can see these addresses belong to one owner."
                % (m["txid"], ", ".join(m["addresses"])), [m["txid"]],
                "Mint one transparent address at a time (mintspark with fromAddresses, or this tool's plan).")

    # R2 mints from different addresses in the same short window (anonymize-all pattern)
    window = opts.burst_blocks
    used = set()
    for i, m in enumerate(mints):
        if m["height"] is None or m["txid"] in used:
            continue
        group = [m]
        for other in mints[i + 1:]:
            if other["height"] is None or other["height"] - m["height"] > window:
                break
            if not set(other["addresses"]) & set().union(*(g["addresses"] for g in group)):
                group.append(other)
        if len(group) > 1:
            used.update(g["txid"] for g in group)
            addrs = sorted(set().union(*(g["addresses"] for g in group)))
            add("HIGH" if len(group) > 2 else "MEDIUM", "burst-mints",
                "%d mints from different addresses within %d blocks" % (len(group), window),
                "Addresses %s were minted at heights %s. Mints that land together point to one owner "
                "(this is what 'Anonymize all' does)." % (", ".join(addrs), ", ".join(str(g["height"]) for g in group)),
                [g["txid"] for g in group], "Space mints from different addresses hours apart, at irregular times.")

    # R3 reward-funded mints
    reward_mints = [m for m in mints if any(p["generated"] for p in m["inputs"])]
    if reward_mints:
        add("LOW", "reward-mints", "%d mint(s) funded by mining/masternode rewards" % len(reward_mints),
            "Reward payouts are public and tied to your masternode. The mint itself is fine; the risk is "
            "what you do after withdrawing.", [m["txid"] for m in reward_mints],
            "Never withdraw to the payout address and never merge withdrawn coins with reward coins.")

    # R4 fixed rhythm
    times = sorted(m["time"] for m in mints if m.get("time"))
    if len(times) >= 4:
        gaps = [b - a for a, b in zip(times, times[1:])]
        mean = sum(gaps) / len(gaps)
        if mean > 0:
            var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
            cv = var ** 0.5 / mean
            if cv < 0.25:
                add("LOW", "regular-interval", "Mints happen at a regular interval",
                    "%d mints, average gap %.1f h, variation %.0f%%." % (len(times), mean / 3600, cv * 100), [],
                    "Randomize when you mint.")
        hours = [time.gmtime(t).tm_hour for t in times]
        best = max(sum(1 for h in hours if circular_hour_gap(h, c) <= 1) for c in range(24))
        if best / len(hours) >= 0.75:
            add("LOW", "same-hour", "Most mints happen at the same time of day",
                "%d of %d mints fall in one 3-hour band (UTC). A daily routine is measurable on chain."
                % (best, len(hours)), [], "Vary the time of day.")

    # R5 withdrawals back to an address that funded a mint (or a reward address)
    for wd in withdrawals:
        if wd["address"] in mint_sources or wd["address"] in reward_addrs:
            add("HIGH", "withdraw-to-deposit-address",
                "Withdrawal to an address that also deposited into Spark",
                "Spend %s pays %s FIRO to %s, which funded mint(s) %s. Deposit and withdrawal are linked "
                "outright." % (wd["txid"], short(wd["sats"]), wd["address"], ", ".join(mint_sources.get(wd["address"], ["(rewards)"]))),
                [wd["txid"]] + mint_sources.get(wd["address"], []),
                "Always withdraw to a fresh address that never held coins.")

    # R6 exact-value withdrawal, R7 quick similar-size round trip
    tol = to_sats(opts.fee_tolerance)
    for wd in withdrawals:
        exact = None
        for m in mints:
            for v in m["values"]:
                if 0 <= v - wd["sats"] <= tol:
                    exact = (m, v)
                    break
            if exact:
                break
        if exact:
            m, v = exact
            common = v in ladder
            add("LOW" if common else "MEDIUM", "exact-value-withdrawal",
                "Withdrawal matches a minted coin value", "Spend %s pays out %s FIRO; mint %s created a %s FIRO "
                "coin. A coin withdrawn untouched carries the fingerprint of its deposit%s."
                % (wd["txid"], short(wd["sats"]), m["txid"], short(v),
                   " (it is a common value, so many deposits match)" if common else ""),
                [wd["txid"], m["txid"]], "Spend inside Spark first, and withdraw amounts that match no deposit.")
            continue
        if wd["height"] is None:
            continue
        for m in mints:
            if m["height"] is None:
                continue
            dt = wd["height"] - m["height"]
            total = sum(m["values"])
            if 0 <= dt <= opts.roundtrip_blocks and total and abs(total - wd["sats"]) <= total * 0.05:
                add("LOW", "quick-round-trip", "Quick in-and-out of similar size",
                    "Mint %s (%s FIRO) and withdrawal %s (%s FIRO) are %d blocks apart. In aggregate this "
                    "pattern was not linkable in the 2026 audit, but a single unusual amount can be."
                    % (m["txid"], short(total), wd["txid"], short(wd["sats"]), dt),
                    [m["txid"], wd["txid"]], "Leave time between deposit and withdrawal and change the amount.")
                break

    # R8 withdrawn coins merged with other coins afterwards (including re-minting them)
    candidate = {m["txid"] for m in mints}
    for t in rpc.call("listtransactions", "*", 100000, 0, True):
        if t.get("txid") and t.get("category") == "send":
            candidate.add(t["txid"])
    for txid in sorted(candidate - spend_txids):
        wtx = rpc.tx(txid)
        if wtx is None:
            continue
        ins = [prevout(v["txid"], v["vout"]) for v in wtx["decoded"]["vin"] if "txid" in v]
        ins = [p for p in ins if p]
        from_wd = [p for p in ins if p["from_spend"]]
        if not from_wd or len(ins) < 2:
            continue
        origins = {("wd", p["txid"]) if p["from_spend"] else ("addr", p["address"]) for p in ins}
        if len(origins) < 2:
            continue
        bad = any(not p["from_spend"] and (p["generated"] or p["address"] in mint_sources) for p in ins)
        add("HIGH" if bad else "MEDIUM", "merged-withdrawal",
            "Withdrawn coins spent together with other coins",
            "Transaction %s spends %d input(s) that came out of Spark together with %d other input(s)%s. "
            "Coins spent together are assumed to share an owner."
            % (txid, len(from_wd), len(ins) - len(from_wd),
               ", including deposit or reward coins" if bad else ""),
            [txid], "Keep each withdrawal separate; never mix it with coins from deposit or reward addresses.")

    # R9 big multi-coin sweeps
    for txid, n in sorted(spend_count.items()):
        if n >= opts.sweep_coins:
            add("MEDIUM", "large-sweep", "One spend used %d Spark coins" % n,
                "Spend %s consumed %d coins. Few people hold that many, so large sweeps narrow down who "
                "sent them." % (txid, n), [txid], "Keep the number of coins you hold moderate; use larger coins.")

    # R10 one address funding several mints over time
    for a, txids in sorted(mint_sources.items()):
        if len(txids) > 1:
            add("LOW", "address-reused-for-mints", "One transparent address funded %d mints" % len(txids),
                "%s funded mints %s. Those deposits are visibly the same person's." % (a, ", ".join(txids)),
                txids, "Receive each payment on a new transparent address.")

    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["code"]))
    summary = {"mints": len(mints), "withdrawal_outputs": len(withdrawals), "spend_txs": len(spend_txids)}
    return findings, summary


def print_findings(findings, summary, out=sys.stdout):
    w = out.write
    w("Audited %d mint(s), %d Spark spend(s), %d withdrawal output(s).\n\n"
      % (summary["mints"], summary["spend_txs"], summary["withdrawal_outputs"]))
    if not findings:
        w("No linkable patterns found in this wallet's own history.\n")
        return
    for f in findings:
        w("[%s] %s\n  %s\n  -> %s\n\n" % (f["severity"], f["title"], f["detail"], f["advice"]))


# --------------------------------------------------------------------------- CLI

def add_rpc_args(p):
    g = p.add_argument_group("connection (same lookup as firo-cli)")
    g.add_argument("--datadir")
    g.add_argument("--conf")
    g.add_argument("--network", choices=sorted(NETWORKS))
    g.add_argument("--rpcconnect")
    g.add_argument("--rpcport")
    g.add_argument("--rpcuser")
    g.add_argument("--rpcpassword")


def make_parser():
    ap = argparse.ArgumentParser(prog="firo-mint-hygiene", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="write a mint schedule for your transparent coins")
    add_rpc_args(p)
    p.add_argument("--out", default="firo-mint-plan.json")
    p.add_argument("--force", action="store_true", help="overwrite a plan that still has pending steps")
    p.add_argument("--style", choices=["common", "random"], default="common",
                   help="common: coins of everyday values (default); random: odd 4-decimal values")
    p.add_argument("--ladder", nargs="+", default=DEFAULT_LADDER, metavar="FIRO",
                   help="coin values to use with --style common")
    p.add_argument("--max-coins", type=int, default=8, help="max Spark coins per address (default 8)")
    p.add_argument("--min-gap-hours", type=float, default=3.0)
    p.add_argument("--max-gap-hours", type=float, default=30.0)
    p.add_argument("--start-now", action="store_true", help="first step may run immediately")
    p.add_argument("--minconf", type=int, default=6)
    p.add_argument("--min-mint", default="0.1", help="leave addresses with less than this alone")
    p.add_argument("--fee-reserve", default="0.01", help="minimum size of the remainder coin that pays the fee")
    p.add_argument("--include-1000", action="store_true", help="also mint outputs of exactly 1000 FIRO")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("audit", help="check this wallet's Spark history for linkable patterns")
    add_rpc_args(p)
    p.add_argument("--unlock", action="store_true", help="ask for the passphrase (listsparkmints needs it)")
    p.add_argument("--burst-blocks", type=int, default=12, help="window for same-time mints (default 12 = ~1 h)")
    p.add_argument("--roundtrip-blocks", type=int, default=72)
    p.add_argument("--fee-tolerance", default="0.01")
    p.add_argument("--sweep-coins", type=int, default=10)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="carry out the next due step of a plan (dry run by default)")
    add_rpc_args(p)
    p.add_argument("plan", nargs="?", default="firo-mint-plan.json")
    p.add_argument("--execute", action="store_true", help="really broadcast (asks you to type MINT)")
    p.add_argument("--yes", action="store_true", help="skip the MINT prompt (for scripts)")
    p.add_argument("--unlock", action="store_true", help="ask for the passphrase and relock afterwards")
    p.add_argument("--step", type=int, help="run this step id instead of the next due one")
    p.add_argument("--now", action="store_true", help="ignore the not-before time")
    return ap


def main(argv=None, rpc=None, out=sys.stdout):
    args = make_parser().parse_args(argv)
    try:
        if rpc is None:
            url, auth, _ = resolve_rpc(args)
            rpc = RpcClient(url, auth)
        if args.cmd == "plan":
            if args.min_gap_hours <= 0 or args.max_gap_hours < args.min_gap_hours:
                raise ToolError("need 0 < --min-gap-hours <= --max-gap-hours")
            if os.path.exists(args.out) and not args.force:
                with open(args.out, encoding="utf-8") as f:
                    old = json.load(f)
                if any(s.get("status") == "pending" for s in old.get("steps", [])):
                    raise ToolError("%s still has pending steps; use --force or another --out" % args.out)
            plan = build_plan(rpc, args)
            save_plan(args.out, plan)
            if args.json:
                out.write(json.dumps(plan, indent=2) + "\n")
            else:
                print_plan(plan, out)
                out.write("\nSaved to %s. Nothing has been sent. Next: firo-mint-hygiene run %s\n"
                          % (args.out, args.out))
            return 0
        if args.cmd == "audit":
            unlocked = False
            try:
                if args.unlock:
                    rpc.call("walletpassphrase", getpass.getpass("Wallet passphrase: "), 60)
                    unlocked = True
                findings, summary = audit(rpc, args)
            finally:
                if unlocked:
                    rpc.call("walletlock")
            if args.json:
                out.write(json.dumps({"summary": summary, "findings": findings}, indent=2) + "\n")
            else:
                print_findings(findings, summary, out)
            return 2 if any(f["severity"] == "HIGH" for f in findings) else 0
        if args.cmd == "run":
            try:
                with open(args.plan, encoding="utf-8") as f:
                    plan = json.load(f)
            except FileNotFoundError:
                raise ToolError("no plan file %s; run `plan` first" % args.plan)
            return run_step(rpc, plan, args.plan, args, out=out)
    except ToolError as e:
        sys.stderr.write("error: %s\n" % e)
        return 1
    except RpcError as e:
        sys.stderr.write("firod refused: %s\n" % e)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
