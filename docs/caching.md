# NOVA-RTL caching

NOVA-RTL stores each completed stage as immutable, content-addressed artifacts. A stage is reusable only when its complete cache identity matches: RTL and design contracts, constraints, analysis views, platform and tool recipes, policies, parent result, and other required input hashes.

On a valid cache hit, NOVA-RTL verifies the stored `StageResult` and every referenced artifact, then loads the saved evidence graph, source map, timing paths, clusters, and ranked opportunities. It does not rerun EDA tools or reconstruct deterministic evidence. A missing, corrupt, or mismatched identity fails closed or causes a fresh stage execution.

This makes interruption recovery and repeated analysis much faster while preserving reproducibility. Final milestone sign-off remains stronger: it independently reconstructs the evidence from the immutable M2 inputs instead of trusting the normal replay cache.
