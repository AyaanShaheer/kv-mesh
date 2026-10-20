# Submission plan for the KV-Mesh manuscript

This note records where the paper stands, what remains before submission, and
which venues were assessed. It exists so the work can be resumed without
re-deriving the analysis.

**Status as of 20 October 2026.** The manuscript is complete, the experiments
are executed, and everything is pushed. The two gates set by the original
draft are: ~~"marked experiments are executed"~~ **done**, and
~~"the related-work review is updated"~~ **still open**.

---

## 1. What the paper currently is

* 17 pages, `paper/kv-mesh.tex` → `paper/kv-mesh.pdf` (build: `make paper`).
* A **prototype and measurement study** of a cache-affinity routing control
  plane for distributed LLM serving.
* Three experimental arms: a control-plane **simulator** (2,880-configuration
  factorial sweep plus four scenario sweeps), a **live-system** arm driving
  the real Go gateway and Python workers over gRPC (54 runs), and
  **microbenchmarks** of the routing decision and the byte pool.
* Archived with the paper: per-run summaries, raw per-request event streams,
  and an environment capture (see `experiments/results/`).

It makes **no** real-model claim. The worker does not run a model, does not
allocate CUDA memory and does not perform a PCIe transfer, and the paper says
so in §2, §4 and §7.3.

## 2. Open gates and known gaps

| # | Item | Severity | Notes |
|---|---|---|---|
| 1 | **Related-work review** | Blocking for every venue | The bibliography is still the original draft's four references plus a self-citation. §2 argues against a thin target. See §4 below. |
| 2 | **Venue format + author metadata** | Blocking | Currently a generic `article` class. Every candidate venue needs a specific class file, and submissions need an email/affiliation. |
| 3 | **ORCID** | Required by ACM venues | Free to obtain; needed before any ACM submission. |
| 4 | **AI Usage Disclosure** | Required by MLSys; not by HotOS | Must describe AI assistance in ideation, literature search, experiment design, code generation and writing. See §5. |
| 5 | **Real-engine arm** | Not blocking | Script exists (`experiments/scripts/bench_realmodel.py`) but could not be brought up in the evaluation environment; documented in `experiments/results/realmodel/README.md`. |
| 6 | **Anonymised version** | Only for double-blind venues | Requires renaming the system and removing the GitHub URL from the self-citation. See §4. |

## 3. Venues assessed

Deadlines are as published at the time of writing; re-check before relying on
them.

| Venue | Deadline | Fit | Work required |
|---|---|---|---|
| **arXiv preprint** (cs.DC / cs.OS) | any time | Good | Format, category choice, metadata. Free, citable, establishes priority. Recommended first step. |
| **HotOS XXI** (SIGOPS, Burghausen, May 2027) | **1 Feb 2027** | **Excellent** — the CFP explicitly solicits position papers, early-stage work, and "deep insights gained from experience with real-world systems" | `acmart` (`sigconf,10pt`), **5 pages** + references, double-blind, renamed system, ORCID. Notification 9 Apr 2027, camera-ready 3 May 2027. |
| **MLSys 2027** (OpenReview, Bellevue) | **30 Oct 2026** | Good venue, weaker odds | `mlsys2025style`, 10 pages + references, OpenReview profile (allow two weeks), AI Usage Disclosure, anonymise. Notification 28 Feb 2027. Artifact Evaluation (ACM badging) available. Note: the **industrial track** explicitly excludes speculative/exploratory projects. |
| **OSDI '27** | 8 Dec 2026 (abstract 1 Dec) | Not ready | Needs real-model results, multi-node scale, and an engine-native baseline. |
| **SOSP 2027** | ~2 Apr 2027 | Ambitious | Same as above. |
| **USENIX ATC 2027** | ~Feb 2027 | Possible | Systems venue; wants production-scale results. |
| **Co-located workshops** (MLSys / EuroSys / SOSP workshops) | various | Good | Short paper plus a live demo of the gateway, workers and metrics endpoint. |

**Recommended ladder:** arXiv preprint → HotOS XXI (Feb 2027) → full systems
conference in the 2027–28 cycle once the real-engine work exists.

## 4. The related-work gap (blocking)

This is the most important remaining item, because it is not only a
citation-list chore: **the paper's central claim is already contested in the
literature**, and the current version does not acknowledge that.

Directly relevant prior art that §2 and the introduction must engage with:

* **llm-d — "Prefix-Cache Aware Routing"** — a *production* implementation of
  cache-affinity routing, claiming "100% precision" and supporting
  prefill/decode disaggregation. This is the closest competitor to the
  paper's idea, and it is not currently cited.
* **BanaServe** (arXiv:2510.13223) — argues that routers can perform
  "purely load aware scheduling, unconstrained by cache placement", i.e.
  directly against the paper's thesis.
* **An Internet for the KV Cache** (arXiv:2608.01526) — a position paper on
  treating inference state as a first-class, networked resource.
* **NetKV** (arXiv:2606.03910) — network-aware decode-instance selection.
* **KVCompass** (arXiv:2610.24179) — a discovery plane for Internet-scale
  prefix caching.
* **ARK** (NIAC '26) — avoiding routing collisions for KV-cache transfer.
* **Prefill-as-a-Service** (arXiv:2604.15039), **FlowKV** (arXiv:2504.03775),
  **Janus** — prefill/decode disaggregation with KV-cache transport.
* Plus the existing four: vLLM automatic prefix caching, LMCache, CacheGen,
  DistServe. **SGLang's RadixAttention** should also be added — it is the
  token-level prefix index that the paper's §3.3 explicitly contrasts
  against, and it is currently uncited.

Why this matters rather than being busywork: §7.2 of the paper *already*
contains the right answer — locality has to be traded against queueing delay,
and a cache match is not unconditionally dominant. That argument is weak
against a strawman of four citations, but it becomes a real contribution
against llm-d and BanaServe, because the paper supplies the measurement that
quantifies the trade-off: the affinity policy keeps 6.4–10.3 percentage points
more of the reusable state in the fast tier, while the p95 queueing delay it
induces grows by two orders of magnitude between a lightly and a heavily
loaded mesh, and the oracle policy's queueing delay grows by three.

Recommended shape for the revision:

1. A **positioning table** in §2: for each closest system, what it does,
   what it assumes, and what it does *not* measure. The honest row for KV-Mesh
   is "measures the locality/queueing trade-off of membership-based affinity
   under explicit staleness and capacity conditions, with an open, reproducible
   artifact."
2. A short paragraph in §7.2 explicitly answering BanaServe's position with
   the paper's own numbers.
3. A **framing change in the abstract and intro**: the contribution becomes
   "quantifying when mesh-level cache locality is worth its cost", not
   "cache-aware routing is a good idea".
4. Reference the artifact's reproducibility as a differentiator: this is one
   of very few papers in this area with archived raw per-request data,
   seeded traces and a rerunnable harness.

## 5. Submissions that require disclosure

MLSys 2027 requires a mandatory **AI Usage Disclosure** section after the
references (not counted toward the page limit) describing in detail how
generative-AI tools were used in ideation, literature search, experiment
design, code generation, data generation and writing.

This matters here: AI assistance was used substantially in the implementation
of the gateway, the router, the worker, the C++ extension, the experiment
harness, and in drafting and revising the manuscript. The disclosure must
describe that honestly and specifically. HotOS does not require such a
section.

## 6. Resuming

```bash
make build && make test          # toolchain and tests
make experiments && make report  # regenerate every table and figure
make paper                        # rebuild the PDF
```

For a submission-specific build, keep the full 17-page paper as the arXiv
version and derive the venue version from it, rather than degrading the
arXiv one. The generated tables live in `paper/tables/` and are overwritten by
`make report`, so venue-specific captions belong in the generator
(`experiments/scripts/make_report.py`), not in the `.tex` files.

## 7. Open questions for the author

* Which venue first? (HotOS is the recommendation; MLSys 2027 is a ten-day
  option that would need the related-work work done immediately.)
* Is there an affiliation to declare, or is the independent-researcher
  framing retained?
* An ORCID is needed for ACM venues.
* For a double-blind venue, a different system name is needed for the
  anonymised version; the arXiv version keeps the original name.
