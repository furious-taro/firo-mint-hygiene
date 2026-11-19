import io
import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import firo_mint_hygiene as fmh  # noqa: E402
from fakenode import COIN, FakeNode, FakeWallet  # noqa: E402

LADDER = {fmh.to_sats(x) for x in fmh.DEFAULT_LADDER}


class Opts:
    style = "common"
    ladder = fmh.DEFAULT_LADDER
    max_coins = 8
    min_gap_hours = 3.0
    max_gap_hours = 30.0
    start_now = False
    minconf = 6
    min_mint = "0.1"
    fee_reserve = "0.01"
    include_1000 = False


class AuditOpts:
    burst_blocks = 12
    roundtrip_blocks = 72
    fee_tolerance = "0.01"
    sweep_coins = 10


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wallet = FakeWallet(height=1_400_000)
        self.node = FakeNode(self.wallet).__enter__()
        self.rpc = fmh.RpcClient(self.node.url, "u:p")

    def tearDown(self):
        self.node.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            code = fmh.main(list(argv) + ["--rpcport", str(self.node.port), "--rpcuser", "u",
                                          "--rpcpassword", "p", "--datadir", self.tmp], out=out)
        finally:
            sys.stderr = old
        return code, out.getvalue(), err.getvalue()


class AmountTests(unittest.TestCase):
    def test_exact_conversion(self):
        self.assertEqual(fmh.to_sats(Decimal("12.34567891")), 1234567891)
        self.assertEqual(fmh.to_sats("0.1"), 10_000_000)
        self.assertEqual(fmh.fmt(1234567891), "12.34567891")
        self.assertEqual(fmh.short(5 * COIN), "5")
        with self.assertRaises(fmh.ToolError):
            fmh.to_sats("0.000000001")

    def test_common_split_invariants(self):
        rng = random.Random(1)
        ladder = sorted(LADDER, reverse=True)
        for _ in range(500):
            total = rng.randint(2_000_000, 50_000 * COIN)
            coins = fmh.split_amount(total, "common", ladder, 6, 1_000_000, rng)
            self.assertEqual(sum(coins), total)
            self.assertLessEqual(len(coins), 6)
            self.assertGreaterEqual(coins[-1], 1_000_000)      # remainder can pay the fee
            for c in coins[:-1]:
                self.assertIn(c, LADDER)

    def test_common_split_keeps_remainder_small(self):
        rng = random.Random(4)
        ladder = sorted(LADDER, reverse=True)
        for total in ("57.123", "29.4", "189.61", "12.5", "0.75"):
            for _ in range(50):
                coins = fmh.split_amount(fmh.to_sats(total), "common", ladder, 8, 1_000_000, rng)
                self.assertLess(coins[-1], COIN, (total, coins))

    def test_common_split_varies(self):
        rng = random.Random(6)
        ladder = sorted(LADDER, reverse=True)
        seen = {tuple(sorted(fmh.split_amount(fmh.to_sats("57.123"), "common", ladder, 8, 1_000_000, rng)))
                for _ in range(50)}
        self.assertGreater(len(seen), 1)

    def test_random_split_invariants(self):
        rng = random.Random(2)
        ladder = sorted(LADDER, reverse=True)
        for _ in range(500):
            total = rng.randint(2_000_000, 5_000 * COIN)
            coins = fmh.split_amount(total, "random", ladder, 5, 1_000_000, rng)
            self.assertEqual(sum(coins), total)
            self.assertLessEqual(len(coins), 5)
            self.assertGreaterEqual(coins[-1], 1_000_000)
            for c in coins[:-1]:
                self.assertEqual(c % 10_000, 0)
                self.assertGreaterEqual(c, min(LADDER))

    def test_schedule_gaps_and_hours(self):
        rng = random.Random(3)
        steps = [{} for _ in range(30)]
        fmh.schedule(steps, 1_760_000_000, 3, 30, rng)
        prev = 1_760_000_000
        for a, b in zip(steps, steps[1:]):
            self.assertGreaterEqual(b["not_before"] - a["not_before"], 3 * 3600 - 1)
            self.assertLessEqual(b["not_before"] - a["not_before"], 30 * 3600 + 1)
            ha, hb = time.gmtime(a["not_before"]).tm_hour, time.gmtime(b["not_before"]).tm_hour
            self.assertGreaterEqual(fmh.circular_hour_gap(ha, hb), 2)
        self.assertGreaterEqual(steps[0]["not_before"], prev + 3 * 3600 - 1)


class ConnectionTests(Base):
    def test_cookie_auth_from_datadir(self):
        node = FakeNode(FakeWallet(), user="__cookie__", password="abc123").__enter__()
        try:
            with open(os.path.join(self.tmp, ".cookie"), "w") as f:
                f.write("__cookie__:abc123\n")
            out = io.StringIO()
            code = fmh.main(["plan", "--datadir", self.tmp, "--rpcport", str(node.port),
                             "--out", os.path.join(self.tmp, "p.json")], out=out)
            self.assertEqual(code, 0, out.getvalue())
        finally:
            node.__exit__(None, None, None)

    def test_conf_file_and_testnet_cookie_path(self):
        os.makedirs(os.path.join(self.tmp, "testnet3"))
        with open(os.path.join(self.tmp, "firo.conf"), "w") as f:
            f.write("testnet=1\n# comment\nrpcport=12345\n")
        with open(os.path.join(self.tmp, "testnet3", ".cookie"), "w") as f:
            f.write("__cookie__:xyz")
        args = fmh.make_parser().parse_args(["audit", "--datadir", self.tmp])
        url, auth, where = fmh.resolve_rpc(args)
        self.assertEqual(url, "http://127.0.0.1:12345/")
        self.assertEqual(auth, "__cookie__:xyz")
        self.assertIn("testnet3", where)

    def test_bad_password_is_reported(self):
        rpc = fmh.RpcClient(self.node.url, "u:wrong")
        with self.assertRaises(fmh.RpcError) as cm:
            rpc.call("getblockcount")
        self.assertIn("authentication failed", str(cm.exception))

    def test_rpc_error_is_parsed(self):
        with self.assertRaises(fmh.RpcError) as cm:
            self.rpc.call("nosuchmethod")
        self.assertEqual(cm.exception.code, -32601)

    def test_unreachable_node(self):
        rpc = fmh.RpcClient("http://127.0.0.1:1/", "u:p", timeout=2)
        with self.assertRaises(fmh.ToolError):
            rpc.call("getblockcount")


class PlanTests(Base):
    def fund(self):
        w = self.wallet
        w.receive("a1", "186.4", height=1_399_000)
        w.receive("a1", "3.21", height=1_399_100)
        w.receive("a2", "57.123", height=1_399_200)
        w.receive("a3", "0.05", height=1_399_300)                 # below min-mint
        w.receive("a4", "1000", height=1_390_000)                 # collateral only
        w.receive("a5", "1000", height=1_390_000)                 # collateral ...
        w.receive("a5", "12.5", height=1_390_000, generated=True)  # ... plus a reward
        w.receive("a6", "9", height=1_399_998)                    # too few confirmations

    def test_plan_structure(self):
        self.fund()
        plan = fmh.build_plan(self.rpc, Opts(), random.Random(5), now=1_760_000_000)
        by = {s["address"]: s for s in plan["steps"]}
        self.assertEqual(set(by), {"a1", "a2", "a5"})
        self.assertEqual(by["a1"]["total"], "189.61000000")
        self.assertEqual(len(by["a1"]["outpoints"]), 2)
        self.assertEqual(sum(fmh.to_sats(v) for v in by["a1"]["outputs"]), fmh.to_sats("189.61"))
        self.assertEqual(by["a5"]["total"], "12.50000000")       # collateral not included
        self.assertEqual(by["a5"]["coinbase_inputs"], 1)
        text = " ".join(plan["warnings"])
        self.assertIn("exactly 1000 FIRO", text)
        self.assertIn("reward", text)
        self.assertIn("same block", text)
        why = {s["what"]: s["why"] for s in plan["skipped"]}
        self.assertIn("below --min-mint", why["a3"])
        self.assertTrue(any("confirmations" in v for v in why.values()))
        times = sorted(s["not_before"] for s in plan["steps"])
        self.assertGreaterEqual(times[0], 1_760_000_000 + 3 * 3600 - 1)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, 3 * 3600 - 1)

    def test_include_1000(self):
        self.fund()
        o = Opts()
        o.include_1000 = True
        plan = fmh.build_plan(self.rpc, o, random.Random(5), now=1_760_000_000)
        self.assertIn("a4", {s["address"] for s in plan["steps"]})

    def test_plan_cli_writes_file_and_refuses_overwrite(self):
        self.fund()
        path = os.path.join(self.tmp, "plan.json")
        code, out, err = self.cli("plan", "--out", path)
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing has been sent", out)
        self.assertNotIn("mintspark", [c[0] for c in self.wallet.calls])
        code, out, err = self.cli("plan", "--out", path)
        self.assertEqual(code, 1)
        self.assertIn("pending steps", err)


class RunTests(Base):
    def make_plan(self):
        w = self.wallet
        self.o1 = w.receive("a1", "186.4", height=1_399_000)
        w.receive("a1", "3.21", height=1_399_100)
        w.receive("a1", "1000", height=1_399_100)                # collateral at the same address
        w.receive("a2", "57.123", height=1_399_200)
        self.path = os.path.join(self.tmp, "plan.json")
        plan = fmh.build_plan(self.rpc, Opts(), random.Random(7), now=1_760_000_000)
        fmh.save_plan(self.path, plan)
        return plan

    def run_args(self, *extra):
        return fmh.make_parser().parse_args(["run", self.path] + list(extra))

    def load(self):
        with open(self.path) as f:
            return json.load(f)

    def test_not_due_does_nothing(self):
        plan = self.make_plan()
        out = io.StringIO()
        fmh.run_step(self.rpc, plan, self.path, self.run_args("--execute", "--yes"), now=1_760_000_000, out=out)
        self.assertIn("not due", out.getvalue())
        self.assertNotIn("mintspark", [c[0] for c in self.wallet.calls])

    def test_dry_run_sends_nothing_and_creates_no_addresses(self):
        plan = self.make_plan()
        out = io.StringIO()
        fmh.run_step(self.rpc, plan, self.path, self.run_args("--now"), out=out)
        names = [c[0] for c in self.wallet.calls]
        self.assertNotIn("mintspark", names)
        self.assertNotIn("getnewsparkaddress", names)
        self.assertNotIn("lockunspent", names)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertEqual(self.load()["steps"][0]["status"], "pending")

    def test_wrong_confirmation_word(self):
        plan = self.make_plan()
        out = io.StringIO()
        code = fmh.run_step(self.rpc, plan, self.path, self.run_args("--now", "--execute"), out=out,
                            ask=lambda prompt: "yes")
        self.assertEqual(code, 1)
        self.assertNotIn("mintspark", [c[0] for c in self.wallet.calls])

    def test_execute_full_address_mint(self):
        plan = self.make_plan()
        step = next(s for s in plan["steps"] if s["address"] == "a1")
        out = io.StringIO()
        code = fmh.run_step(self.rpc, plan, self.path, self.run_args("--step", str(step["id"]), "--execute"),
                            now=1_760_000_000, out=out, ask=lambda p: "MINT")
        self.assertEqual(code, 0, out.getvalue())
        calls = self.wallet.calls
        mint = [p for m, p in calls if m == "mintspark"][0]
        recipients, subtract, from_addrs = mint
        self.assertIs(subtract, False)
        self.assertEqual(from_addrs, ["a1"])
        self.assertEqual(len(set(recipients)), len(step["outputs"]))
        self.assertEqual([r["amount"] for r in recipients.values()], step["outputs"])
        # collateral was locked for the call and unlocked after
        locks = [p for m, p in calls if m == "lockunspent"]
        self.assertEqual(locks[0][0], False)
        self.assertEqual(locks[1][0], True)
        self.assertEqual(self.wallet.locked, set())
        # the mint spent exactly the planned outputs, made no transparent change,
        # kept the round coins exact and took the fee from the last (remainder) coin
        saved = self.load()
        done = [s for s in saved["steps"] if s["id"] == step["id"]][0]
        tx = self.wallet.txs[done["txids"][0]]
        spent = {(v["txid"], v["vout"]) for v in tx["vin"]}
        self.assertEqual(spent, {(o["txid"], o["vout"]) for o in step["outpoints"]})
        self.assertFalse(any("address" in o for o in tx["vout"]))
        minted = [o["sats"] for o in tx["vout"]]
        planned = [fmh.to_sats(v) for v in step["outputs"]]
        self.assertEqual(minted[:-1], planned[:-1])
        self.assertEqual(minted[-1], planned[-1] - self.wallet.fee)
        # the 1000 FIRO output is still there
        self.assertIn(("a1", 1000 * COIN), {(o["address"], o["sats"]) for _, _, o, _ in self.wallet.utxos()})
        self.assertEqual(done["status"], "done")
        other = [s for s in saved["steps"] if s["status"] == "pending"][0]
        self.assertGreaterEqual(other["not_before"], 1_760_000_000 + 3 * 3600)

    def test_late_run_pushes_overdue_steps_out(self):
        plan = self.make_plan()
        late = max(s["not_before"] for s in plan["steps"]) + 86400
        fmh.run_step(self.rpc, plan, self.path, self.run_args("--execute", "--yes"), now=late, out=io.StringIO())
        saved = self.load()
        pending = [s for s in saved["steps"] if s["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(pending[0]["not_before"], late + 3 * 3600)
        # running again right away does nothing
        out = io.StringIO()
        fmh.run_step(self.rpc, saved, self.path, self.run_args("--execute", "--yes"), now=late + 60, out=out)
        self.assertIn("not due", out.getvalue())
        self.assertEqual(len([1 for m, _ in self.wallet.calls if m == "mintspark"]), 1)

    def test_new_funds_at_address_are_locked_out(self):
        plan = self.make_plan()
        step = next(s for s in plan["steps"] if s["address"] == "a2")
        self.wallet.receive("a2", "5", height=1_400_000)
        fmh.run_step(self.rpc, plan, self.path, self.run_args("--step", str(step["id"]), "--execute", "--yes"),
                     out=io.StringIO())
        self.assertIn(("a2", 5 * COIN), {(o["address"], o["sats"]) for _, _, o, _ in self.wallet.utxos()})
        self.assertEqual(self.wallet.locked, set())

    def test_spent_since_planning_aborts(self):
        plan = self.make_plan()
        step = next(s for s in plan["steps"] if s["address"] == "a1")
        self.wallet.send([(self.o1, 0)], [("elsewhere", "186")])
        with self.assertRaises(fmh.ToolError):
            fmh.run_step(self.rpc, plan, self.path, self.run_args("--step", str(step["id"]), "--execute", "--yes"),
                         out=io.StringIO())
        self.assertNotIn("mintspark", [c[0] for c in self.wallet.calls])

    def test_wrong_chain(self):
        plan = self.make_plan()
        self.wallet.chain = "test"
        with self.assertRaises(fmh.ToolError):
            fmh.run_step(self.rpc, plan, self.path, self.run_args("--now"), out=io.StringIO())

    def test_unlock_and_relock(self):
        plan = self.make_plan()
        self.wallet.encrypted = True
        self.wallet.unlocked = False
        fmh.run_step(self.rpc, plan, self.path, self.run_args("--now", "--execute", "--yes", "--unlock"),
                     out=io.StringIO(), askpass=lambda p: "correct horse")
        names = [c[0] for c in self.wallet.calls]
        self.assertLess(names.index("walletpassphrase"), names.index("mintspark"))
        self.assertGreater(names.index("walletlock"), names.index("mintspark"))
        self.assertFalse(self.wallet.unlocked)

    def test_cli_run_missing_plan(self):
        self.path = os.path.join(self.tmp, "none.json")
        code, out, err = self.cli("run", self.path)
        self.assertEqual(code, 1)
        self.assertIn("run `plan` first", err)


class AuditTests(Base):
    def codes(self):
        findings, summary = fmh.audit(self.rpc, AuditOpts())
        self.summary = summary
        return {f["code"]: f for f in findings}, findings

    def test_clean_history(self):
        w = self.wallet
        r1 = w.receive("a1", "50", height=1_000_000, time_=1_700_000_000)
        r2 = w.receive("a2", "20", height=1_010_000, time_=1_700_400_000)
        w.mint([(r1, 0)], ["20", "20", "9.99"], height=1_000_100, time_=1_700_030_000)
        w.mint([(r2, 0)], ["10", "9.99"], height=1_020_000, time_=1_703_000_000)
        w.spark_spend(2, [("fresh1", "13.37")], height=1_100_000)
        codes, findings = self.codes()
        self.assertEqual(findings, [], findings)
        self.assertEqual(self.summary["mints"], 2)

    def test_detects_linkable_patterns(self):
        w = self.wallet
        r1 = w.receive("a1", "30", height=1_000_000)
        r2 = w.receive("a2", "20", height=1_000_000)
        r3 = w.receive("a3", "40", height=1_000_000)
        rw = w.receive("mn", "14.7", height=1_000_000, generated=True)
        rw2 = w.receive("mn", "14.7", height=1_000_500, generated=True)
        r3b = w.receive("a3", "7", height=1_000_000)
        m1 = w.mint([(r1, 0), (r2, 0)], ["49.99"], height=1_100_000)          # two addresses in one mint
        m2 = w.mint([(r3, 0)], ["39.99"], height=1_100_004)                   # same hour as m1
        m3 = w.mint([(rw, 0)], ["14.6913"], height=1_200_000)                 # reward-funded
        m4 = w.mint([(r3b, 0)], ["6.99"], height=1_300_000)                   # a3 again
        s1 = w.spark_spend(1, [("a1", "14.6912")], height=1_250_000)          # back to deposit addr, exact value
        s2 = w.spark_spend(12, [("shop", "120")], height=1_260_000)           # big sweep
        t = w.send([(s1, 0), (rw2, 0)], [("x", "29")], height=1_270_000)      # merge with reward coin
        codes, findings = self.codes()
        self.assertEqual(codes["multi-address-mint"]["severity"], "HIGH")
        self.assertIn(m1, codes["multi-address-mint"]["txids"])
        self.assertEqual(set(codes["burst-mints"]["txids"]), {m1, m2})
        self.assertIn(m3, codes["reward-mints"]["txids"])
        self.assertEqual(codes["withdraw-to-deposit-address"]["severity"], "HIGH")
        self.assertEqual(codes["exact-value-withdrawal"]["severity"], "MEDIUM")
        self.assertIn(m3, codes["exact-value-withdrawal"]["txids"])
        self.assertIn(s2, codes["large-sweep"]["txids"])
        self.assertEqual(codes["merged-withdrawal"]["severity"], "HIGH")
        self.assertEqual(codes["merged-withdrawal"]["txids"], [t])
        self.assertEqual(set(codes["address-reused-for-mints"]["txids"]), {m2, m4})
        self.assertEqual(findings[0]["severity"], "HIGH")

    def test_common_value_withdrawal_is_low(self):
        w = self.wallet
        r = w.receive("a1", "30.5", height=1_000_000)
        w.mint([(r, 0)], ["20", "10.49"], height=1_100_000)
        w.spark_spend(1, [("fresh", "19.9999")], height=1_300_000)
        codes, _ = self.codes()
        self.assertEqual(codes["exact-value-withdrawal"]["severity"], "LOW")

    def test_quick_round_trip(self):
        w = self.wallet
        r = w.receive("a1", "77.7", height=1_000_000)
        w.mint([(r, 0)], ["77.69"], height=1_100_000)
        w.spark_spend(1, [("fresh", "75.5")], height=1_100_030)
        codes, _ = self.codes()
        self.assertIn("quick-round-trip", codes)

    def test_regular_rhythm(self):
        w = self.wallet
        for i in range(5):
            r = w.receive("a%d" % i, "10", height=1_000_000)
            w.mint([(r, 0)], ["9.99"], height=1_000_000 + i * 288, time_=1_700_000_000 + i * 86400)
        codes, _ = self.codes()
        self.assertIn("regular-interval", codes)
        self.assertIn("same-hour", codes)

    def test_locked_wallet_message(self):
        self.wallet.encrypted = True
        self.wallet.unlocked = False
        with self.assertRaises(fmh.ToolError) as cm:
            fmh.audit(self.rpc, AuditOpts())
        self.assertIn("--unlock", str(cm.exception))

    def test_cli_exit_code_and_json(self):
        w = self.wallet
        r1 = w.receive("a1", "30", height=1_000_000)
        r2 = w.receive("a2", "20", height=1_000_000)
        w.mint([(r1, 0), (r2, 0)], ["49.99"], height=1_100_000)
        code, out, err = self.cli("audit", "--json")
        self.assertEqual(code, 2, err)
        data = json.loads(out)
        self.assertEqual(data["findings"][0]["code"], "multi-address-mint")


if __name__ == "__main__":
    unittest.main()
