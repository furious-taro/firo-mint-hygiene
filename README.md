# firo-mint-hygiene

Move transparent FIRO into Spark without the patterns that let an outside observer link your coins.

Spark's cryptography hides what happens inside the pool. What it cannot hide is how coins go in
and come out. The independent Spark audit posted on the Firo forum in October 2026
([topic 4388](https://forum.firo.org/t/proposal-independent-privacy-research-firo-spark/4388))
found that most links come from coin handling: one owner on both sides, masternode rewards cycled
through Spark, withdrawn coins merged together, exact coin values withdrawn untouched, large sweeps
and fixed daily routines. Earlier, [topic 4022](https://forum.firo.org/t/thoughts-on-traceability-when-anonymizing-all-transparent-amounts-at-once-in-firo-should-add-partial-options/4022)
asked for something better than "Anonymize all", including amounts in a "predefined bucket system".

`firo-mint-hygiene` is a small command-line tool for people who run `firod` (masternode operators,
servers, anyone using `firo-cli`). It has three commands:

| command | what it does |
|---|---|
| `plan`  | reads your transparent coins and writes a mint schedule: **one transparent address per mint**, the balance split into **common coin values** (100, 50, 20, 10 ...), and the mints **spread out over hours at irregular times** |
| `run`   | carries out the next due step of the plan. **Dry run by default**; with `--execute` you type `MINT` to confirm |
| `audit` | reads your wallet's own Spark history and flags the linkable patterns from the audit, with advice |

One Python 3 file, standard library only (Python 3.8+). It talks to your own node over JSON-RPC and
never touches keys: signing stays inside firod.

## Quick start

```sh
git clone https://github.com/furious-taro/firo-mint-hygiene
cd firo-mint-hygiene
python3 firo_mint_hygiene.py audit                 # what has already leaked?
python3 firo_mint_hygiene.py plan                  # write firo-mint-plan.json (sends nothing)
python3 firo_mint_hygiene.py run                   # dry run of the next due step
python3 firo_mint_hygiene.py run --execute         # broadcast it (asks you to type MINT)
```

Run `run --execute` again whenever the next step is due (the command tells you when), by hand or from
cron / Task Scheduler. Each call executes **at most one** step. If you come back late, the remaining
steps are pushed out so they never fire back to back.

Connection settings are found the way `firo-cli` finds them: `rpcuser`/`rpcpassword`/`rpcport` from
`firo.conf`, otherwise the `.cookie` file in the data directory (`~/.firo`, `%APPDATA%\firo`,
`~/Library/Application Support/firo`; `testnet3/` and `regtest/` subfolders for `--network test|regtest`).
Override with `--datadir --conf --rpcconnect --rpcport --rpcuser --rpcpassword`. Encrypted wallet: add
`--unlock` (asks for the passphrase, relocks afterwards).

## Example (against the test suite's fake node)

```
$ firo-mint-hygiene plan
Mint plan (main, height 1400000), 3 step(s)

Step 1  not before 2026-11-20 09:42 UTC  [pending]
  from   aOther...  (1 output(s), 57.123 FIRO)
  coins  10, 1, 20, 20, 1, 0.1, 5 + remainder 0.023 (pays the fee)
Step 2  not before 2026-11-21 07:08 UTC  [pending]
  from   a8Rw...  (2 output(s), 189.61 FIRO)
  coins  20, 100, 2, 10, 5, 50, 2 + remainder 0.61 (pays the fee)
Step 3  not before 2026-11-21 13:51 UTC  [pending]
  from   aMNpayout...  (2 output(s), 29.4 FIRO, 2 reward output(s))
  coins  1, 0.2, 20, 2, 0.1, 5, 1 + remainder 0.1 (pays the fee)

Warnings:
  ! 3 transparent addresses. 'Anonymize all' / automintspark would mint all of them in the same block,
    which links them. This plan spaces them 3.0-30.0 hours apart.
  ! aMNpayout... holds 1 output(s) of exactly 1000 FIRO (masternode collateral?). They are left out and
    will be locked with lockunspent while this address is minted. ...
  ! aMNpayout... is funded by 2 mining/masternode reward output(s). ... never send Spark withdrawals
    back to this address or merge them with its coins.

$ firo-mint-hygiene run --now
Step 1: mint 57.123 FIRO from aOther... into 8 Spark coin(s)
  RPC: mintspark {"sm1...": {"amount": "10.00000000", "memo": ""}, ...} false ["aOther..."]
DRY RUN: nothing was sent. Add --execute to broadcast this step.
```

## How a step is executed

Each step is one call of firod's own RPC:

```
mintspark {"<spark addr 1>":{"amount":"10.00000000","memo":""}, ...} false ["<one transparent address>"]
```

* `fromAddresses` holds **one** address, so a mint never combines coins from two addresses. A plain
  `mintspark` without it, or the GUI with coin control off, can merge several addresses in one
  transaction, which links them for good.
* The step mints **every** planned output at that address, so there is no transparent change.
  Other outputs that arrived at the address after planning (and any 1000 FIRO collateral) are
  locked with `lockunspent` for the duration of the call and unlocked right after.
* **How the fee is paid.** When the selected coins are minted in full, firod's
  `CreateSparkMintTransactions` (src/spark/sparkwallet.cpp) takes the fee out of the mint value, and
  because outputs are filled in order, the fee comes off the **last** output. The plan always puts
  the small odd "remainder" coin last, so the round coins stay exactly round.
* Before broadcasting, `run` re-reads the address. If a planned output has been spent, or the
  plan was made for another chain, it stops.
* Spark recipients are encrypted on chain, so the tool reuses your wallet's existing Spark addresses
  (`getallsparkaddresses`) and only creates new ones (`getnewsparkaddress`) when it needs more
  distinct keys for the `mintspark` object. A dry run creates nothing.

### Why common values (and not odd ones)?

A mint's value is public. After it, the value is hidden, until a coin is withdrawn untouched: then
the withdrawal amount (minus a fee) matches the mint, and the audit found exactly that
fingerprint. If your coins are 10s and 20s, a matching withdrawal points to thousands of deposits,
not one. `--style random` (odd 4-decimal values) is there if you prefer it, but common values are the
default for that reason. The odd remainder coin is small by design. Spend it inside Spark before any
withdrawal.

## What `audit` looks for

| code | severity | pattern |
|---|---|---|
| `multi-address-mint` | HIGH | one mint spent coins from several transparent addresses |
| `burst-mints` | MEDIUM/HIGH | mints from different addresses within `--burst-blocks` (default 12, about 1 h): the "Anonymize all" footprint |
| `withdraw-to-deposit-address` | HIGH | a Spark spend paid an address that funded one of your mints or received rewards |
| `merged-withdrawal` | MEDIUM/HIGH | coins that came out of Spark were later spent together with other coins (HIGH if with deposit or reward coins) |
| `exact-value-withdrawal` | MEDIUM (LOW for common values) | a withdrawal equals one of your minted coin values minus up to `--fee-tolerance` |
| `large-sweep` | MEDIUM | one spend used `--sweep-coins` (default 10) or more Spark coins |
| `quick-round-trip` | LOW | a similar-size withdrawal within `--roundtrip-blocks` of a mint |
| `reward-mints` | LOW | mints funded by mining/masternode rewards (fine in itself; advice on what not to do next) |
| `regular-interval`, `same-hour` | LOW | mints on a fixed rhythm or at the same time of day |
| `address-reused-for-mints` | LOW | one transparent address funded several mints |

`audit` exits with code 2 when it finds a HIGH item, and `--json` gives machine-readable output.
It uses `listsparkmints`, `listsparkspends`, `listtransactions`, `gettransaction` and
`decoderawtransaction` only. No `-txindex` is needed, because every input it looks at belongs to your wallet.

## Honest limits: what this does NOT protect against

* **The deposit itself is public.** Every mint shows which transparent address paid how much. If the
  coins came from an exchange, the exchange knows you deposited into Spark. This tool cannot hide that.
  It only stops separate addresses being tied to each other and makes later withdrawals harder to match.
* **Splitting does not hide the total from someone watching one address.** All coins of one address are
  minted in one transaction on purpose. Splitting one address into several mints over time would only
  create change outputs that chain the mints together.
* **What you do after the mint matters more.** Withdrawing to an old address, merging withdrawn
  coins, withdrawing an untouched coin, or cycling masternode rewards back to the payout address will
  undo the work. `audit` can only point these out after the fact.
* **Network-level privacy is out of scope.** Your IP when broadcasting, Tor/I2P, and who can see your
  node are not handled here.
* **Timing is only as random as the gaps you choose.** If you always run the command right after
  lunch, that is a routine again. Running it from a scheduler at the planned times avoids that.
* **Small anonymity set for unusual amounts.** A 5,000 FIRO coin is common in the ladder but rare on
  chain. Large holders stand out whatever they do.
* **`audit` sees only this wallet.** It cannot see mints you made from another wallet, payments
  where someone else merged coins you sent, or chain-wide clustering. It is a checklist, not a
  chain-analysis engine.
* **Not yet run against a live node.** The test suite uses a fake firod that follows the RPC
  rules read from Firo Core v0.14.18.2 source. An end-to-end regtest/testnet run with the official
  release is the first item on the roadmap below. Until then, please try it on testnet first.
* No GUI, and it does not change the Qt wallet's own "Anonymize all" / auto-anonymize behaviour.
  If you use Firo-QT with auto-anonymize on, it may mint new funds before this tool's schedule does.

## Relationship to Firo Core

Recent Firo Core already lets you mint an exact amount in the Qt wallet ("Make Private...", PR
[#1891](https://github.com/firoorg/firo/pull/1891)) or with `mintspark`. This tool does not replace
that. It adds what neither does: a per-address plan with common coin values, spacing over time,
headless use for `firod` servers, guarding masternode collateral, and an audit of past history. If the
ideas prove useful, they can move into the wallet itself.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

31 offline tests run against `tests/fakenode.py`, a real HTTP JSON-RPC server with Basic/cookie
auth whose wallet follows firod's rules for `mintspark`, `lockunspent`, `listunspent`,
`listsparkmints` (refuses while locked) and friends.

## Roadmap

1. End-to-end run on regtest and testnet with the official Firo Core release; txids and transcript in this README.
2. Packaging (`pip install` / single-file release), a `--wait` mode for people without a scheduler.
3. A short guide on the Firo docs site for masternode operators.

## License

MIT, see [LICENSE](LICENSE).
