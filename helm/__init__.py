"""helm — the personal knowledge home for people who build with coding agents.

One home (~/.helm) that knows your projects across every harness, holds the
authored knowledge chain per project (premises/heuristics/lexicon/prd/journal/
evals/archive), unifies the typed personal-knowledge store (priors/lexicon/
memory) behind one resolver, and keeps it all clean (drain + drift + lineage).

helm is an INDEX/OVERLAY over stores that already exist — harness session
homes, per-project memory dirs, recall indexes, the repos themselves. It
references; it never duplicates. Every projection is read-only as truth: an
edit lands in the source, and the projection re-derives.
"""

__version__ = "0.1.0-alpha"
